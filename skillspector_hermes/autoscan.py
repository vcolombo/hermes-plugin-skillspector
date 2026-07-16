# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Config-gated ``pre_tool_call`` gate for extension installs.

NOTE: This terminal-command parser is the FALLBACK gate, used only on stock
Hermes builds that lack the ``pre_plugin_install`` / ``pre_mcp_add`` lifecycle
hooks. It is best-effort defense-in-depth over an inherently ambiguous surface
(a shell string) and is intentionally FROZEN — do not extend it to chase new
shell forms. The authoritative gate is ``install_gate`` (canonical, post-parse,
TOCTOU-free); prefer fixing/expanding that. Anything this parser cannot classify
cleanly escalates to approval (fail-closed).

Best-effort, defense-in-depth: when the agent shells out through the ``terminal``
tool to install a Hermes **plugin** or **MCP server**, scan the target with
SkillSpector first and — *fail-closed* — escalate anything not-clean to Hermes'
existing human-approval gate.

This is deliberately NOT airtight:

* **TOCTOU** — it scans a mutable reference (``owner/repo``, ``pkg@latest``)
  before Hermes independently fetches it to install; the installed bytes may
  differ from the scanned ones. Airtight scanning belongs in the installer
  (staged/quarantined artifact), which is what Hermes' own Skills Hub does — and
  is why **skills are intentionally out of scope here** (already guarded by
  ``tools.skills_guard``).
* It only recognises a **single, plain** ``hermes …`` command; anything wrapped
  or compound escalates to approval rather than being parsed heuristically.

Hermes core **catches hook exceptions and proceeds (fail-open)**. So this
callback must catch every error itself and return an ``approve`` directive on
failure — otherwise a bug here would silently let an unscanned install through.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import multiprocessing
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import tools

logger = logging.getLogger("skillspector_hermes.autoscan")

_TERMINAL_TOOL = "terminal"
_DEFAULT_TIMEOUT_S = 120

# Shell wrappers that can execute a hermes install indirectly (e.g. `sh -c '…'`).
_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "env", "eval", "xargs", "nohup", "time", "sudo"}

# Stdio MCP runners whose real artifact lives in `--args`, not the runner name.
# `hermes mcp add foo --command npx --args @scope/server` → scan @scope/server.
_MCP_RUNNERS = {"npx", "uvx", "pipx", "node", "python", "python3", "deno", "bun", "sh", "bash"}

# Runner options that load/execute extra code the payload scan would miss
# (`--require=/x`, `-r x`, `--import x`, `--loader …`, `-e '<code>'`). The
# non-flag payload filter drops these, so a config pairing a clean artifact with
# one of them scans only the clean file while the runner runs the injected one.
_LOADER_OPTS = frozenset(
    {
        "--require",
        "-r",
        "--import",
        "--loader",
        "--experimental-loader",
        "--preload",
        "--eval",
        "-e",
    }
)
# An option value ending in one of these (or a data: URI) is executable code.
_CODE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".py", ".wasm", ".sh")

# Compound / redirection / variable shell — escalate rather than parse a single command.
_COMPOUND = ("&&", "||", ";", "|", "&", "`", "$", ">", "<", "\n")

# Whitespace-tolerant intent, used only for wrapped or untokenizable commands.
_INTENT_RE = re.compile(
    r"\bhermes\b.*\b(?:plugins\s+(?:install|update)|mcp\s+(?:add|install))\b",
    re.DOTALL,
)

# A ref SkillSpector can actually fetch: a URL or a `owner/repo` GitHub shorthand.
_URL_RE = re.compile(r"^(?:https?|git\+https?|file|ssh)://|^git@")
_GH_SHORTHAND_RE = re.compile(r"^([\w.-]+)/([\w.-]+)(?:/[\w.-]+)*$")


@dataclass(frozen=True)
class Target:
    """A classified terminal command. ``kind`` drives the policy in :func:`evaluate`."""

    kind: str  # "plugin" | "mcp_local" | "remote" | "unparseable"
    ref: str | None = None


@dataclass(frozen=True)
class Config:
    enabled: bool
    use_llm: bool
    timeout_s: int

    @classmethod
    def from_mapping(cls, m: dict[str, Any]) -> Config:
        return cls(
            enabled=bool(m.get("enabled", False)),
            use_llm=bool(m.get("use_llm", True)),
            timeout_s=cls._coerce_timeout(m.get("scan_timeout_s")),
        )

    @staticmethod
    def _coerce_timeout(value: object) -> int:
        # A bad/typo'd timeout must NOT collapse `enabled` — it would silently
        # disable the whole gate. Fall back to the default instead.
        try:
            seconds = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return _DEFAULT_TIMEOUT_S
        return seconds if seconds > 0 else _DEFAULT_TIMEOUT_S


def _first_non_flag(tokens: list[str]) -> str | None:
    for t in tokens:
        if not t.startswith("-"):
            return t
    return None


def _value_after(tokens: list[str], flag: str) -> str | None:
    """Value token immediately after ``flag`` (``--url X`` -> ``X``); None if absent/flag."""
    i = tokens.index(flag) + 1
    return tokens[i] if i < len(tokens) and not tokens[i].startswith("-") else None


def _is_hermes(tok: str) -> bool:
    # A bare/path-qualified `hermes` binary, or the `python -m hermes_cli[.main]`
    # module entry point. When the anchor is not token 0 (e.g. behind `python -m`),
    # parse_install_target escalates it as unparseable rather than trusting it.
    return (
        tok == "hermes"
        or tok.endswith("/hermes")
        or tok == "hermes_cli"
        or tok.startswith("hermes_cli.")
    )


def _has_compound(command: str) -> bool:
    """True if the command chains, redirects, or expands — not a single plain command."""
    return any(op in command for op in _COMPOUND)


def has_code_injecting_option(args: list[str]) -> bool:
    """True if *args* carries an option that loads/executes code a payload scan
    would miss — a known loader flag (``--require``/``-r``/``--import``/…) or any
    attached ``--opt=value`` whose value is a script file or ``data:`` URI. Fail
    closed when present rather than scanning only the visible non-flag artifact.
    """
    for a in args:
        if not isinstance(a, str) or not a.startswith("-"):
            continue
        opt, _, val = a.partition("=")
        if opt in _LOADER_OPTS:
            return True
        if val and (val.startswith("data:") or val.endswith(_CODE_SUFFIXES)):
            return True
    return False


def _mcp_local_target(after: list[str]) -> str | None:
    """The single scannable payload of a stdio MCP server, or ``None``.

    Enumerate every executable input Hermes will actually run: a *custom*
    ``--command`` binary (a known runner like npx/node/python is the interpreter,
    not a payload, so it is skipped) plus every non-flag artifact in ``--args``.
    Exactly one such payload is scannable; zero (a bare runner) or more than one
    fail closed to approval (``None``) rather than scanning one and trusting the
    rest — e.g. ``--command /tmp/evil.sh --args /tmp/clean.py`` runs the unscanned
    script, and ``--args --import ./clean.mjs ./evil.mjs`` executes a second file.
    A launch-environment flag (``--env``/``-e``) can inject code that no single
    artifact scan would see (``--env NODE_OPTIONS=--require=/tmp/evil.js``), so any
    presence escalates.
    """
    if "--env" in after or "-e" in after:
        return None
    if has_code_injecting_option(after):
        return None  # a loader/require/import option runs code the payload scan misses
    payloads: list[str] = []
    if "--command" in after:
        cmd = _first_non_flag(after[after.index("--command") + 1 :])
        if cmd is not None and cmd not in _MCP_RUNNERS:
            payloads.append(cmd)
    if "--args" in after:
        payloads.extend(t for t in after[after.index("--args") + 1 :] if not t.startswith("-"))
    return payloads[0] if len(payloads) == 1 else None


def _classify(tokens: list[str]) -> tuple[str, str | None] | None:
    """Structurally detect a hermes extension install from the token stream.

    Returns ``(kind, ref)`` or ``None``. Detection is token-based (not raw
    substring) so extra whitespace never hides an install and a mere mention of
    the phrase inside a quoted argument (one token) is not misread as a command.
    """
    hi = next((i for i, t in enumerate(tokens) if _is_hermes(t)), None)
    if hi is None:
        return None
    rest = tokens[hi + 1 :]

    if "plugins" in rest:
        after = rest[rest.index("plugins") + 1 :]
        # Find the verb by token, not by "first non-flag": a global option value
        # (`plugins --profile work install …`) would otherwise be read as the verb
        # and the install would slip through.
        verb = next((t for t in after if t in ("install", "update")), None)
        if verb is None:
            return None  # plugins list/enable/… — not an install
        ref = _first_non_flag(after[after.index(verb) + 1 :])
        return ("plugin", ref)  # ref may be None (`plugins update` == all)

    if "mcp" in rest:
        after = rest[rest.index("mcp") + 1 :]
        verb = next((t for t in after if t in ("add", "install")), None)
        if verb is None:
            return None  # mcp list/remove/… — not an install
        after_verb = after[after.index(verb) + 1 :]
        name = _first_non_flag(after_verb)
        # Only flags *before* --args are transport options; everything after it is
        # the runner's own argv (`--command echo --args foo --url bar`) and must
        # not be read as a transport. Match Hermes' precedence: --url > --preset >
        # --command, so a command that pairs a remote URL with a local runner is
        # classified remote (unscannable) rather than scanning the local runner.
        head = after_verb[: after_verb.index("--args")] if "--args" in after_verb else after_verb
        if "--url" in head:
            return ("remote", _value_after(after_verb, "--url") or name)
        if "--preset" in head:
            preset = _value_after(after_verb, "--preset")
            return ("remote", f"preset:{preset}" if preset else name)
        if "--command" in head:
            return ("mcp_local", _mcp_local_target(after_verb))  # scan the artifact, not runner
        return ("remote", name)  # `mcp install <catalog>` / bare add

    return None


def parse_install_target(command: object) -> Target | None:
    """Classify *command*. ``None`` means "not an extension install — ignore".

    Returns a :class:`Target` only when the command structurally invokes a
    hermes plugin/MCP install. A plain ``hermes …`` install is parsed; anything
    wrapped, chained, or variable-expanded becomes ``Target("unparseable")`` so
    the policy escalates rather than guesses.
    """
    if not isinstance(command, str) or "hermes" not in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes around a hermes-install command — can't trust it.
        return Target("unparseable") if _INTENT_RE.search(command) else None

    classified = _classify(tokens)
    if classified is None:
        # _classify can't see the install because it is hidden by shell indirection:
        # a wrapper quoting it (`sh -c '…'`, `command sh -c '…'`) or a variable
        # standing in for the binary (`H=hermes; $H plugins install …`). Escalate
        # when the install phrase is present AND there is any sign of that trickery
        # — compound/variable metacharacters, or a shell-wrapper token (by basename,
        # so `/bin/sh` and nested `command sh` are caught). A benign mention with
        # none of these (`git commit -m "…hermes plugins install…"`) stays None: the
        # phrase is just one quoted arg to a non-shell program.
        if _INTENT_RE.search(command) and (
            _has_compound(command) or any(os.path.basename(t) in _SHELL_WRAPPERS for t in tokens)
        ):
            return Target("unparseable")
        return None

    hi = next(i for i, t in enumerate(tokens) if _is_hermes(t))
    if hi != 0 or _has_compound(command):
        return Target("unparseable")  # env-prefix/wrapper before hermes, or compound shell

    kind, ref = classified
    return Target(kind, ref)


def _approve(message: str, key: str) -> dict[str, str]:
    # Always scope the approval. An empty key lets Hermes fall back to the shared
    # `terminal` rule, so a single `[a]lways` would waive the gate for every later
    # install — the whole point of the gate. A per-target key keeps `[a]lways` narrow.
    return {"action": "approve", "message": message, "rule_key": f"skillspector_scan:{key}"}


def _summary(verdict: dict[str, Any], ref: str | None) -> str:
    return (
        f"SkillSpector flagged {ref!r}: severity={verdict.get('severity')}, "
        f"risk_score={verdict.get('risk_score')}. "
        f"{verdict.get('recommendation') or 'Review findings before installing.'} "
        "Approve to install anyway."
    )


def _scannable_source(target: Target) -> str | None:
    """Resolve ``target.ref`` to something SkillSpector can actually fetch.

    SkillSpector accepts a URL or an existing local artifact — not a bare plugin
    shorthand (``owner/repo``), an npm/uvx/pipx package name, or an installed
    plugin's local name. A ``owner/repo`` plugin shorthand is expanded to its
    GitHub URL (a *mutable* reference — the TOCTOU caveat in the module docstring
    applies). Returns ``None`` when the ref resolves to no fetchable source, so
    the caller escalates it as source-unavailable instead of firing a scan that
    would only ever error.
    """
    ref = target.ref
    if not ref:
        return None
    if _URL_RE.match(ref) or ref.startswith(("/", "./", "../", "~")) or os.path.exists(ref):
        return ref
    if target.kind == "plugin":
        m = _GH_SHORTHAND_RE.match(ref)
        if m:
            return f"https://github.com/{m.group(1)}/{m.group(2)}"
    return None


def _resolve_local(source: str, workdir: object) -> str | None:
    """Make a scannable source absolute so the scan sees the file Hermes will run.

    URLs and expanded shorthand are location-independent. A *relative* local path
    (``./server.py``) must be resolved against the terminal's execution directory,
    not the Hermes process cwd — otherwise a clean same-named file in one dir can
    vouch for a malicious one in the other. Returns ``None`` when the path is
    relative and no trustworthy ``workdir`` is available, so the caller fails
    closed to approval rather than scanning the wrong file.
    """
    if _URL_RE.match(source):
        return source
    if source.startswith("~"):
        return os.path.expanduser(source)
    if os.path.isabs(source):
        return source
    if isinstance(workdir, str) and workdir:
        return os.path.join(workdir, source)
    return None


def _rule_key(target: Target, command: str) -> str:
    """Stable per-target approval key; the whitespace-normalized command as fallback."""
    return target.ref or " ".join(command.split())


def evaluate(
    target: Target,
    verdict: dict[str, Any] | None,
    key: str | None = None,
    llm_required: bool = False,
) -> dict[str, str] | None:
    """Map a scan verdict to a directive. ``None`` = allow (clean only)."""
    key = key or target.ref or ""
    if not isinstance(verdict, dict) or "error" in verdict:
        return _approve(
            f"SkillSpector scan did not complete for {target.ref!r}; approve install manually?",
            key,
        )
    if verdict.get("safe_to_install") is True:
        # A configured semantic scan that silently degraded to static-only (host
        # LLM down/misconfigured) must NOT auto-approve — that would quietly turn
        # the gate the operator asked for into a weaker one. Require positive
        # confirmation the LLM pass ran; anything else (false/absent) escalates.
        if llm_required and verdict.get("llm_used") is not True:
            return _approve(
                f"SkillSpector scanned {target.ref!r} statically only — the requested "
                f"semantic pass did not run (llm_used={verdict.get('llm_used')!r}, "
                f"scan_mode={verdict.get('scan_mode')!r}); approve install manually?",
                key,
            )
        return None
    return _approve(_summary(verdict, target.ref), key)


def scan_reason(ctx: object, cfg: Config, source: str) -> str | None:
    """Scan *source* and map the verdict to a block reason (or ``None`` = allow).

    Used by the post-parse install gate, where a non-None return blocks the
    install. Fail-closed and a clean two-state policy:

    * ``use_llm: false`` — a static scan; a clean static verdict allows.
    * ``use_llm: true`` — the operator asked for the semantic pass, so a clean
      verdict is only allowed with positive confirmation it ran (``llm_used`` is
      True). Anything else — an outage, or no host LLM bound in this context —
      blocks (escalates to approval) rather than silently downgrading to static.

    An errored/incomplete/timed-out scan always blocks.
    """
    verdict = _scan(ctx, cfg, source)
    if not isinstance(verdict, dict) or "error" in verdict:
        return f"SkillSpector scan did not complete for {source!r}"
    if verdict.get("safe_to_install") is not True:
        return _summary(verdict, source)
    if cfg.use_llm and verdict.get("llm_used") is not True:
        return (
            f"requested semantic scan of {source!r} did not run "
            f"(use_llm=true, llm_used={verdict.get('llm_used')!r}, "
            f"scan_mode={verdict.get('scan_mode')!r}); approve manually"
        )
    return None


def _scan_worker(conn: Any, args: dict[str, Any], host_llm: object) -> None:
    """Child entry point: run the scan and pipe back the JSON report string."""
    try:
        conn.send(tools.skillspector_scan(args, host_llm=host_llm))
    except Exception as exc:  # noqa: BLE001 — mirror the never-raise contract into the child
        conn.send(json.dumps({"error": str(exc), "type": type(exc).__name__}))
    finally:
        conn.close()


def _scan(ctx: object, cfg: Config, source: str) -> dict[str, Any] | None:
    """Run the scan in a killable worker under a bounded timeout. ``None`` on timeout.

    A hung/looping scan (network-wedged LLM call, pathological input) must not
    outlive the timeout — a Python thread cannot be force-killed, so the work
    runs in a child **process** that is ``terminate()``-d on timeout. Uses the
    ``fork`` start method: fork inherits the live ``host_llm`` object and the
    import state (so the scan behaves exactly as in-process), which ``spawn``
    cannot — it would pickle the unpicklable host LLM. Where ``fork`` is
    unavailable (macOS/Windows dev), fall back to an unkillable thread.
    ponytail: fork worker; if hung scans still accumulate on a fork-less host,
    move the fallback to a spawn+static subprocess.
    """
    args = {"target": source, "use_llm": cfg.use_llm, "output_format": "json"}
    host_llm = getattr(ctx, "llm", None)
    try:
        mp = multiprocessing.get_context("fork")
    except ValueError:
        return _scan_in_thread(args, host_llm, cfg.timeout_s, source)

    parent_conn, child_conn = mp.Pipe(duplex=False)
    proc = mp.Process(target=_scan_worker, args=(child_conn, args, host_llm), daemon=True)
    proc.start()
    child_conn.close()  # only the child holds the send end now; lets recv see EOF
    try:
        if not parent_conn.poll(cfg.timeout_s):
            logger.warning(
                "skillspector auto-scan timed out after %ss for %r; terminating worker",
                cfg.timeout_s,
                source,
            )
            return None
        try:
            raw = parent_conn.recv()
        except EOFError:  # worker died without sending (e.g. killed/segfault)
            return None
        return json.loads(raw)
    finally:
        parent_conn.close()
        if proc.is_alive():
            proc.terminate()
        proc.join(1)


def _scan_in_thread(
    args: dict[str, Any], host_llm: object, timeout_s: int, source: str
) -> dict[str, Any] | None:
    """Fork-less fallback: bounded wait, but the worker thread is not killable."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(tools.skillspector_scan, args, host_llm=host_llm)
    try:
        return json.loads(future.result(timeout=timeout_s))
    except concurrent.futures.TimeoutError:
        logger.warning("skillspector auto-scan timed out after %ss for %r", timeout_s, source)
        return None
    finally:
        executor.shutdown(wait=False)  # do not block on an orphaned scan


def _decide(ctx: object, cfg: Config, tool_name: str, args: object) -> dict[str, str] | None:
    if tool_name != _TERMINAL_TOOL:
        return None
    command = args.get("command") if isinstance(args, dict) else None
    workdir = args.get("workdir") if isinstance(args, dict) else None
    target = parse_install_target(command)
    if target is None:
        return None
    key = _rule_key(target, command)  # command is a str whenever target is not None
    if target.kind == "remote":
        return _approve(
            "Installing a remote MCP endpoint — no source to scan. "
            "Approve after validating the config?",
            key,
        )
    if target.kind == "unparseable":
        return _approve(
            "Extension install detected but not safely parseable; approve manually?",
            key,
        )
    source = _scannable_source(target)
    if source is None:
        # A bare package name / plugin shorthand-less name: SkillSpector can't
        # fetch it, so a scan would only error — escalate as source-unavailable.
        return _approve(
            f"Extension install {target.ref!r} has no fetchable source to scan "
            "(package name or unresolved reference); approve manually?",
            key,
        )
    resolved = _resolve_local(source, workdir)
    if resolved is None:
        # A relative local target with no trustworthy workdir: scanning it in the
        # Hermes cwd could vouch for a different file than the terminal executes.
        return _approve(
            f"Extension install target {target.ref!r} is a relative path and the "
            "terminal workdir is unknown; approve manually?",
            key,
        )
    return evaluate(target, _scan(ctx, cfg, resolved), key, cfg.use_llm)


def make_hook(ctx: object, cfg: Config) -> Callable[..., dict[str, str] | None]:
    """Build the ``pre_tool_call`` callback. Registered only when enabled."""

    def _pre_tool_call(*, tool_name: str = "", args: object = None, **_kwargs: object):
        try:
            return _decide(ctx, cfg, tool_name, args)
        except Exception:  # noqa: BLE001 — fail closed; a raise would fail OPEN in Hermes
            logger.exception("skillspector auto-scan hook errored; escalating to approval")
            cmd = args.get("command") if isinstance(args, dict) else None
            key = " ".join(cmd.split()) if isinstance(cmd, str) and cmd.strip() else "unparsed"
            return _approve("SkillSpector auto-scan errored; approve this install manually?", key)

    return _pre_tool_call


def load_config(ctx: object) -> Config:
    """Read this plugin's ``auto_scan`` config block; disabled on any failure."""
    try:
        from hermes_cli.config import cfg_get
        from hermes_cli.config import load_config as _load_hermes_config

        manifest = getattr(ctx, "manifest", None)
        plugin_id = getattr(manifest, "key", None) or getattr(manifest, "name", None)
        if not plugin_id:
            return Config.from_mapping({})
        raw = cfg_get(
            _load_hermes_config(), "plugins", "entries", plugin_id, "auto_scan", default={}
        )
        return Config.from_mapping(raw if isinstance(raw, dict) else {})
    except Exception:  # noqa: BLE001 — no hermes_cli / bad config -> stay disabled
        logger.debug("skillspector auto-scan config unavailable; hook disabled", exc_info=True)
        return Config.from_mapping({})
