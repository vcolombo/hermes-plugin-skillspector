# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Auto-scan gate: command classification, fail-closed policy, hook wiring."""

from __future__ import annotations

import importlib
import json

import pytest

PKG = "skillspector_hermes"


def _autoscan():
    return importlib.import_module(f"{PKG}.autoscan")


def _tools():
    return importlib.import_module(f"{PKG}.tools")


# -- parse_install_target: ignore everything that is not an extension install --


@pytest.mark.parametrize(
    "cmd",
    [
        "pip install yt-dlp",  # the original false trigger
        "apt install ripgrep",
        "hermes chat -q hi",
        "hermes plugins list",
        "hermes mcp remove foo",
        "hermes skills install a/b",  # skills are out of scope (guarded elsewhere)
        "",
        None,
        42,
    ],
)
def test_non_extension_installs_are_ignored(plugin, cmd):
    assert _autoscan().parse_install_target(cmd) is None


# -- parse_install_target: real plugin installs are classified + ref extracted --


@pytest.mark.parametrize(
    ("cmd", "ref"),
    [
        ("hermes plugins install owner/repo", "owner/repo"),
        ("hermes plugins install --force https://github.com/a/b", "https://github.com/a/b"),
        ("hermes -p reviewer plugins install owner/repo", "owner/repo"),
        ("hermes plugins update foo", "foo"),
        # Global option between the subcommand and the verb (value must not be
        # mistaken for the verb).
        ("hermes plugins --profile work install owner/repo", "owner/repo"),
    ],
)
def test_plugin_installs_classified(plugin, cmd, ref):
    t = _autoscan().parse_install_target(cmd)
    assert t.kind == "plugin" and t.ref == ref


def test_global_flag_before_mcp_verb_is_handled(plugin):
    t = _autoscan().parse_install_target(
        "hermes mcp --profile work add foo --url https://e.com/mcp"
    )
    assert t.kind == "remote" and t.ref == "https://e.com/mcp"


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m hermes_cli.main plugins install owner/repo",
        "python3 -m hermes_cli plugins install owner/repo",
    ],
)
def test_python_module_entry_point_is_not_bypassed(plugin, cmd):
    # The module entry point is a real Hermes install path; it must not slip
    # through as None. Anchor is not token 0, so it escalates fail-closed.
    assert _autoscan().parse_install_target(cmd).kind == "unparseable"


def test_mcp_env_injection_escalates(plugin, monkeypatch):
    # `--env NODE_OPTIONS=--require=/tmp/evil.js` loads unscanned code at launch;
    # scanning only the --args artifact would auto-allow it.
    cmd = (
        "hermes mcp add x --command node "
        "--env NODE_OPTIONS=--require=/tmp/evil.js --args /tmp/clean.js"
    )
    t = _autoscan().parse_install_target(cmd)
    assert t.kind == "mcp_local" and t.ref is None

    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail(
            "env-injecting MCP install must not be scanned as one artifact"
        ),
    )
    assert _hook()(tool_name="terminal", args={"command": cmd})["action"] == "approve"


def test_plugins_update_all_has_no_ref(plugin):
    t = _autoscan().parse_install_target("hermes plugins update")
    assert t.kind == "plugin" and t.ref is None


@pytest.mark.parametrize(
    "cmd",
    [
        "hermes plugins  install evil/repo",  # double space
        "hermes plugins\tinstall evil/repo",  # tab
        "hermes  plugins   install   evil/repo",  # ragged spacing
    ],
)
def test_extra_whitespace_does_not_bypass_the_gate(plugin, cmd):
    # Regression: a substring intent gate missed these, silently skipping the scan.
    t = _autoscan().parse_install_target(cmd)
    assert t.kind == "plugin" and t.ref == "evil/repo"


@pytest.mark.parametrize(
    "cmd",
    [
        'git commit -m "add hermes plugins install gate"',
        "echo 'hermes plugins install support shipped'",
        'grep "hermes mcp add" notes.md',
    ],
)
def test_mentioning_the_phrase_in_a_quoted_arg_is_not_an_install(plugin, cmd):
    # Regression: these benign commands used to escalate to a spurious approval.
    assert _autoscan().parse_install_target(cmd) is None


# -- parse_install_target: MCP local vs remote ---------------------------------


def test_mcp_local_bare_path_command_is_scannable(plugin):
    t = _autoscan().parse_install_target("hermes mcp add foo --command /usr/local/bin/thing")
    assert t.kind == "mcp_local" and t.ref == "/usr/local/bin/thing"


@pytest.mark.parametrize(
    ("cmd", "ref"),
    [
        # The artifact is the --args package/script, NOT the runner in --command.
        (
            "hermes mcp add gh --command npx --args @modelcontextprotocol/server-github",
            "@modelcontextprotocol/server-github",
        ),
        ("hermes mcp add s --command npx --args -y @scope/server", "@scope/server"),
        ("hermes mcp add s --command python --args /opt/mcp/server.py", "/opt/mcp/server.py"),
    ],
)
def test_mcp_local_scans_the_args_artifact_not_the_runner(plugin, cmd, ref):
    t = _autoscan().parse_install_target(cmd)
    assert t.kind == "mcp_local" and t.ref == ref


def test_mcp_local_bare_runner_has_no_scannable_target(plugin):
    # `--command npx` with no --args: nothing resolvable -> ref None -> _decide escalates.
    t = _autoscan().parse_install_target("hermes mcp add s --command npx")
    assert t.kind == "mcp_local" and t.ref is None


@pytest.mark.parametrize(
    "cmd",
    [
        "hermes mcp add foo --url https://example.com/mcp",
        "hermes mcp add foo --preset github",
        "hermes mcp install some-catalog-name",
    ],
)
def test_mcp_remote_has_no_source(plugin, cmd):
    assert _autoscan().parse_install_target(cmd).kind == "remote"


# -- parse_install_target: compound / wrapped commands escalate, never parse ----


@pytest.mark.parametrize(
    "cmd",
    [
        "hermes plugins install a && rm -rf /",
        "hermes plugins install a; echo hi",
        "echo x | hermes plugins install a",
        "hermes plugins install $PKG",
        "FOO=1 hermes plugins install a",  # env prefix -> not a plain hermes command
        "echo hermes plugins install a",  # wrapper -> first token not hermes
        "sh -c 'hermes plugins install a'",
        "/bin/sh -c 'hermes plugins install evil/repo'",  # path-qualified wrapper
        "/usr/bin/bash -c 'hermes mcp add x --url https://e.com/mcp'",
        "sudo hermes plugins install a",  # sudo prefix -> not a plain hermes command
        "command sh -c 'hermes plugins install evil/repo'",  # nested wrapper
        "xargs sh -c 'hermes plugins install evil/repo'",  # nested via xargs
        "env sh -c 'hermes mcp add x --command ./evil.sh'",
        "H=hermes; $H plugins install evil/repo",  # variable-indirected binary
        "HERMES=hermes && $HERMES plugins install evil/repo",
    ],
)
def test_compound_or_wrapped_escalates(plugin, cmd):
    assert _autoscan().parse_install_target(cmd).kind == "unparseable"


def test_multi_artifact_runner_argv_is_ambiguous_and_escalates(plugin, monkeypatch):
    # Regression: `--args --import ./clean.mjs ./evil.mjs` scanned only ./clean.mjs
    # while node executes ./evil.mjs. >1 artifact -> ambiguous -> fail closed.
    t = _autoscan().parse_install_target(
        "hermes mcp add x --command node --args --import ./clean.mjs ./evil.mjs"
    )
    assert t.kind == "mcp_local" and t.ref is None

    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail("ambiguous multi-artifact argv must not be scanned"),
    )
    out = _hook()(
        tool_name="terminal",
        args={"command": "hermes mcp add x --command node --args --import ./clean.mjs ./evil.mjs"},
    )
    assert out["action"] == "approve"


# -- evaluate: clean allows, everything else escalates (fail-closed) ------------


def test_evaluate_clean_allows(plugin):
    a = _autoscan()
    assert a.evaluate(a.Target("plugin", "x"), {"safe_to_install": True}) is None


def test_evaluate_unsafe_escalates_with_scoped_rule_key(plugin):
    a = _autoscan()
    d = a.evaluate(
        a.Target("plugin", "owner/repo"),
        {"safe_to_install": False, "severity": "high", "risk_score": 80},
    )
    assert d["action"] == "approve"
    assert d["rule_key"] == "skillspector_scan:owner/repo"


@pytest.mark.parametrize("verdict", [None, {"error": "boom"}, "not-a-dict", {}])
def test_evaluate_failed_or_missing_verdict_escalates(plugin, verdict):
    a = _autoscan()
    assert a.evaluate(a.Target("plugin", "x"), verdict)["action"] == "approve"


# -- static-only downgrade: a requested semantic pass that didn't run escalates -


@pytest.mark.parametrize(
    "verdict",
    [
        {"safe_to_install": True, "llm_used": False, "scan_mode": "static-only"},
        {"safe_to_install": True},  # llm_used absent -> unconfirmed -> escalate
        {"safe_to_install": True, "llm_used": None},
    ],
)
def test_requested_llm_pass_that_did_not_run_escalates(plugin, verdict):
    a = _autoscan()
    d = a.evaluate(a.Target("plugin", "owner/repo"), verdict, "owner/repo", llm_required=True)
    assert d["action"] == "approve"
    assert d["rule_key"] == "skillspector_scan:owner/repo"


def test_confirmed_llm_pass_that_is_clean_allows(plugin):
    a = _autoscan()
    verdict = {"safe_to_install": True, "llm_used": True, "scan_mode": "semantic"}
    assert a.evaluate(a.Target("plugin", "x"), verdict, "x", llm_required=True) is None


def test_static_only_clean_allows_when_llm_not_requested(plugin):
    # use_llm: false -> static-only clean is exactly what the operator asked for.
    a = _autoscan()
    verdict = {"safe_to_install": True, "llm_used": False, "scan_mode": "static-only"}
    assert a.evaluate(a.Target("plugin", "x"), verdict, "x", llm_required=False) is None


def test_hook_escalates_when_configured_llm_scan_degrades_to_static(plugin, monkeypatch):
    a = _autoscan()
    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda args, **k: json.dumps(
            {"safe_to_install": True, "llm_used": False, "scan_mode": "static-only"}
        ),
    )
    hook = a.make_hook(ctx=object(), cfg=a.Config(enabled=True, use_llm=True, timeout_s=5))
    out = hook(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out["action"] == "approve"


# -- Config robustness: a bad timeout must not silently disable the gate -------


@pytest.mark.parametrize("bad", ["2m", "120s", None, "", [], "abc"])
def test_bad_scan_timeout_keeps_gate_enabled(plugin, bad):
    # Regression: int("2m") raised -> load_config's except returned a DISABLED
    # Config, silently turning the gate off despite `enabled: true`.
    cfg = _autoscan().Config.from_mapping({"enabled": True, "scan_timeout_s": bad})
    assert cfg.enabled is True
    assert cfg.timeout_s == 120  # falls back to the default, never 0 or a crash


@pytest.mark.parametrize(("value", "expected"), [(30, 30), (0, 120), (-5, 120)])
def test_scan_timeout_coercion(plugin, value, expected):
    cfg = _autoscan().Config.from_mapping({"scan_timeout_s": value})
    assert cfg.timeout_s == expected


# -- source resolution: what SkillSpector can actually fetch -------------------
# _scannable_source is unit-tested directly (the scan itself runs in a forked
# worker, so observing the passed target through a shared dict is unreliable).


def _never_scans(monkeypatch):
    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail("nothing fetchable to scan — must not call SkillSpector"),
    )


@pytest.mark.parametrize(
    ("target", "source"),
    [
        # Regression: `owner/repo` was passed raw; SkillSpector only fetches URLs,
        # so the scan errored and normal plugin installs fell back to approval.
        (("plugin", "owner/repo"), "https://github.com/owner/repo"),
        (("plugin", "owner/repo/subdir"), "https://github.com/owner/repo"),
        (("plugin", "https://github.com/a/b"), "https://github.com/a/b"),
        (("plugin", "git@github.com:a/b.git"), "git@github.com:a/b.git"),
        (("mcp_local", "/opt/mcp/server.py"), "/opt/mcp/server.py"),
    ],
)
def test_scannable_source_resolves_fetchable_refs(plugin, target, source):
    a = _autoscan()
    assert a._scannable_source(a.Target(*target)) == source


@pytest.mark.parametrize(
    "target",
    [
        ("plugin", "foo"),  # `plugins update foo` — a bare installed name
        ("plugin", None),  # `plugins update` (all)
        ("mcp_local", "@scope/server"),  # npm package name, not a fetchable artifact
        ("mcp_local", "server-github"),  # bare package name
        ("mcp_local", None),
    ],
)
def test_scannable_source_returns_none_for_unfetchable_refs(plugin, target):
    a = _autoscan()
    assert a._scannable_source(a.Target(*target)) is None


def test_plugin_update_by_name_is_source_unavailable_and_not_scanned(plugin, monkeypatch):
    _never_scans(monkeypatch)
    out = _hook()(tool_name="terminal", args={"command": "hermes plugins update foo"})
    assert out["action"] == "approve"
    assert out["rule_key"] == "skillspector_scan:foo"


def test_mcp_package_spec_is_source_unavailable_and_not_scanned(plugin, monkeypatch):
    # Regression: `@scope/server` is an npm name, not a fetchable artifact.
    _never_scans(monkeypatch)
    out = _hook()(
        tool_name="terminal",
        args={"command": "hermes mcp add gh --command npx --args @scope/server"},
    )
    assert out["action"] == "approve"
    assert out["rule_key"] == "skillspector_scan:@scope/server"


# -- MCP transport precedence: --url wins over --command; --args argv is opaque -


def test_url_paired_with_local_command_is_classified_remote(plugin):
    # Regression: `--command` was checked first, so a remote endpoint bundled with
    # a benign local runner was scanned as local and auto-allowed. Hermes installs
    # the --url endpoint; the gate must treat it as unscannable remote.
    t = _autoscan().parse_install_target(
        "hermes mcp add x --url https://evil.example/mcp --command /bin/echo"
    )
    assert t.kind == "remote" and t.ref == "https://evil.example/mcp"


def test_flags_after_args_are_not_read_as_transport(plugin):
    # `--url` here belongs to the runner's argv (after --args), not the transport,
    # so it stays mcp_local (not remote). Two artifacts also make it ambiguous.
    t = _autoscan().parse_install_target(
        "hermes mcp add x --command echo --args payload --url not-a-transport"
    )
    assert t.kind == "mcp_local"


def test_single_arg_after_args_still_resolves(plugin):
    t = _autoscan().parse_install_target("hermes mcp add x --command node --args ./server.js")
    assert t.kind == "mcp_local" and t.ref == "./server.js"


def test_custom_runner_plus_args_is_ambiguous_and_escalates(plugin, monkeypatch):
    # Regression: a non-runner --command (`/tmp/evil.sh`) is itself executed but
    # was ignored; only the --args file was scanned. Two payloads -> fail closed.
    t = _autoscan().parse_install_target(
        "hermes mcp add x --command /tmp/evil.sh --args /tmp/clean.py"
    )
    assert t.kind == "mcp_local" and t.ref is None

    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail("custom runner + arg is ambiguous — must not scan just one"),
    )
    out = _hook()(
        tool_name="terminal",
        args={"command": "hermes mcp add x --command /tmp/evil.sh --args /tmp/clean.py"},
    )
    assert out["action"] == "approve"


def test_custom_runner_alone_is_scannable(plugin):
    t = _autoscan().parse_install_target("hermes mcp add x --command /usr/local/bin/thing")
    assert t.kind == "mcp_local" and t.ref == "/usr/local/bin/thing"


@pytest.mark.parametrize(
    "cmd",
    [
        # `--require`/`--import`/`-r` load code the non-flag payload scan misses.
        "hermes mcp add x --command node --args --require=/tmp/evil.js /tmp/clean.js",
        "hermes mcp add x --command node --args -r /tmp/evil.js /tmp/clean.js",
        "hermes mcp add x --command node --args --import /tmp/evil.mjs /tmp/clean.js",
        # `-c` marks a command string, not a scannable file (metachar-free here so
        # this isolates the -c detection from the compound-escalation path).
        "hermes mcp add x --command bash --args -c /opt/evil.sh",
        "hermes mcp add x --command python --args -c import_module",
        # attached short forms with no space: -c<code>, -r<file>
        "hermes mcp add x --command python --args -cimport_module /tmp/clean.py",
        "hermes mcp add x --command node --args -r/tmp/evil.js /tmp/clean.js",
    ],
)
def test_loader_option_in_mcp_args_is_unscannable(plugin, monkeypatch, cmd):
    # Regression (fallback parser): a loader option must not let a clean artifact
    # vouch for an injected one -> ref None -> escalate, never scan.
    t = _autoscan().parse_install_target(cmd)
    assert t.kind == "mcp_local" and t.ref is None

    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail(
            "loader-option MCP install must not be scanned as one artifact"
        ),
    )
    assert _hook()(tool_name="terminal", args={"command": cmd})["action"] == "approve"


# -- relative targets resolve against the terminal workdir, else escalate -------


def test_resolve_local_joins_relative_against_workdir(plugin):
    import os

    a = _autoscan()
    assert a._resolve_local("./server.py", "/work") == os.path.join("/work", "./server.py")
    assert a._resolve_local("sub/x.py", "/work") == os.path.join("/work", "sub/x.py")
    assert a._resolve_local("/abs/x.py", None) == "/abs/x.py"
    assert a._resolve_local("https://github.com/a/b", None) == "https://github.com/a/b"
    assert a._resolve_local("~/x.py", None) == os.path.expanduser("~/x.py")
    assert a._resolve_local("./server.py", None) is None  # relative + no workdir -> escalate
    assert a._resolve_local("./server.py", "") is None


def test_relative_target_without_workdir_escalates(plugin, monkeypatch):
    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda *a, **k: pytest.fail("relative target with unknown workdir must not be scanned"),
    )
    out = _hook()(
        tool_name="terminal",
        args={"command": "hermes mcp add x --command node --args ./server.js"},
    )
    assert out["action"] == "approve"


# -- scoped approvals: no directive may fall back to the shared terminal key ----


@pytest.mark.parametrize(
    ("cmd", "rule_key"),
    [
        (
            "hermes mcp add foo --url https://example.com/mcp",
            "skillspector_scan:https://example.com/mcp",
        ),
        ("hermes mcp add foo --preset github", "skillspector_scan:preset:github"),
        ("hermes mcp install some-catalog", "skillspector_scan:some-catalog"),
        (
            "hermes plugins install a && rm -rf /",
            "skillspector_scan:hermes plugins install a && rm -rf /",
        ),
        ("hermes plugins update", "skillspector_scan:hermes plugins update"),
    ],
)
def test_no_ref_escalations_still_get_a_scoped_rule_key(plugin, monkeypatch, cmd, rule_key):
    # Regression: ref=None omitted rule_key, so Hermes reused the shared `terminal`
    # rule — one `[a]lways` there waived the gate for later installs.
    _never_scans(monkeypatch)
    out = _hook()(tool_name="terminal", args={"command": cmd})
    assert out["action"] == "approve"
    assert out["rule_key"] == rule_key


def test_hook_error_path_uses_a_command_scoped_key(plugin, monkeypatch):
    # A bug *outside* the scan (classification) hits the hook's own except path,
    # which keys off the raw command. (Scan-internal errors are caught in the
    # worker and escalate via evaluate with the target-scoped key instead.)
    a = _autoscan()

    def _boom(*args, **kwargs):
        raise RuntimeError("classify exploded")

    monkeypatch.setattr(a, "parse_install_target", _boom)
    out = _hook()(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out["action"] == "approve"
    assert out["rule_key"] == "skillspector_scan:hermes plugins install owner/repo"


def test_scan_timeout_kills_worker_and_escalates(plugin, monkeypatch):
    # A hung scan must not auto-allow: on timeout _scan returns None -> escalate.
    import time

    def _hang(*args, **kwargs):
        time.sleep(30)
        return json.dumps({"safe_to_install": True})

    monkeypatch.setattr(_tools(), "skillspector_scan", _hang)
    a = _autoscan()
    hook = a.make_hook(ctx=object(), cfg=a.Config(enabled=True, use_llm=False, timeout_s=1))
    started = time.monotonic()
    out = hook(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out["action"] == "approve"  # fail-closed on timeout
    assert time.monotonic() - started < 10  # returned near the 1s timeout, not 30s


# -- make_hook: end-to-end policy through the callback -------------------------


def _hook(cfg=None):
    a = _autoscan()
    return a.make_hook(ctx=object(), cfg=cfg or a.Config(enabled=True, use_llm=False, timeout_s=5))


def test_hook_allows_clean_install(plugin, monkeypatch):
    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda args, **k: json.dumps({"safe_to_install": True, "risk_score": 0}),
    )
    out = _hook()(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out is None


def test_hook_escalates_unsafe_install(plugin, monkeypatch):
    monkeypatch.setattr(
        _tools(),
        "skillspector_scan",
        lambda args, **k: json.dumps({"safe_to_install": False, "severity": "critical"}),
    )
    out = _hook()(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out["action"] == "approve"


def test_hook_fails_closed_when_scan_raises(plugin, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(_tools(), "skillspector_scan", _boom)
    out = _hook()(tool_name="terminal", args={"command": "hermes plugins install owner/repo"})
    assert out["action"] == "approve"  # fail-closed: Hermes core would fail-OPEN on a raise


def test_hook_ignores_non_terminal_tool_and_non_install(plugin, monkeypatch):
    def _must_not_run(*a, **k):
        raise AssertionError("scan must not run for non-install commands")

    monkeypatch.setattr(_tools(), "skillspector_scan", _must_not_run)
    hook = _hook()
    assert hook(tool_name="editor", args={"command": "hermes plugins install x"}) is None
    assert hook(tool_name="terminal", args={"command": "pip install yt-dlp"}) is None


# -- register wiring: hook installed only when enabled + hooks supported --------


class _Ctx:
    llm = None

    def __init__(self):
        self.hooks = []

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, name, cb):
        self.hooks.append(name)


def test_register_installs_hook_when_enabled(plugin, monkeypatch):
    a = _autoscan()
    monkeypatch.setattr(a, "load_config", lambda ctx: a.Config(True, True, 5))
    monkeypatch.setattr(plugin, "_supported_hooks", lambda: {"pre_tool_call"})
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.hooks == ["pre_tool_call"]


def test_register_skips_hook_when_disabled(plugin, monkeypatch):
    a = _autoscan()
    monkeypatch.setattr(a, "load_config", lambda ctx: a.Config(False, True, 5))
    ctx = _Ctx()
    plugin.register(ctx)
    assert ctx.hooks == []
