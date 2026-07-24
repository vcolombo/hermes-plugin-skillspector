# Post-Parse Install Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the leaky `pre_tool_call` shell-command parser with two authoritative, upstreamable Hermes-core hooks (`pre_plugin_install`, `pre_mcp_add`) that hand the scanner canonical, already-parsed install args — while feature-detecting so the plugin still runs a frozen best-effort parser on stock (un-patched) Hermes.

**Architecture:** Two subsystems delivered together. **Phase 1** patches Hermes core (`/home/vcolombo/git/personal/hermes-agent`, → upstream PR to `NousResearch/hermes-agent`): two new lifecycle hooks fired at the sole install choke points, blocking on a returned reason exactly like the existing `validate_mcp_server_entry` gate. **Phase 2** rewrites this plugin to register those hooks when the running Hermes supports them (detected via `VALID_HOOKS` membership — the same native-vs-bridge feature-detection pattern already in `bridge.py`), else fall back to the existing (now frozen) terminal-parsing hook. The post-parse path scans the **cloned files on disk** (plugins) / the **resolved `server_config`** (MCP) — canonical data, no shell guessing, no TOCTOU.

**Tech Stack:** Python 3, Hermes plugin API (`register_hook`/`invoke_hook`), SkillSpector `run_scan` (via existing `tools.skillspector_scan`), pytest, ruff. Fork-based killable scan worker (existing `autoscan._scan`).

## Global Constraints

- **Package is `skillspector_hermes`, never `skillspector`** — a top-level `skillspector` package would shadow the installed SkillSpector distribution. (CLAUDE.md invariant.)
- **`skillspector_scan` never raises** — every failure path returns a JSON `{"error": ...}` string. New scan callers must preserve this. (CLAUDE.md invariant.)
- **The plugin never imports the Hermes `agent` package** and calls the host LLM by duck typing (`host_llm.complete(...)`). Importing `hermes_cli.plugins` for `VALID_HOOKS` feature-detection is allowed (already imports `hermes_cli.config`); guard it in try/except so absence never breaks load.
- **Report honesty fields** (`llm_requested`/`llm_available`/`llm_used`/`scan_mode`) stay meaningful; a static-only scan is never silently sold as a full semantic one.
- **Hermes core hooks must be generic and vendor-neutral** — no `import skillspector*` in core (so the PR is mergeable upstream). Mirror the existing `validate_mcp_server_entry(name, entry) -> list[str]` block-verdict shape.
- Repo commands (this plugin): `uvx ruff format skillspector_hermes tests`, `uvx ruff check skillspector_hermes tests`, `uvx --with pytest pytest -q`. Line length 100. No AI attribution in commits.
- Hermes-core repo commands run in `/home/vcolombo/git/personal/hermes-agent` with its own `pytest`.

---

## File Structure

**Phase 1 — Hermes core (`/home/vcolombo/git/personal/hermes-agent`):**
- Modify `hermes_cli/plugins.py` — add two names + contract comments to `VALID_HOOKS`.
- Modify `hermes_cli/plugins_cmd.py:_install_plugin_core` — fire `pre_plugin_install`, block → `PluginOperationError`.
- Modify `hermes_cli/mcp_config.py:_save_mcp_server` — fire `pre_mcp_add`, merge block reasons into the existing `validate_mcp_server_entry` rejection.
- Add `hermes_cli/_hook_reasons.py` (tiny shared flatten helper) OR inline; plan inlines to keep the PR surface minimal.
- Tests in `tests/` of that repo.

**Phase 2 — Plugin (`/home/vcolombo/git/personal/hermes-plugin-skillspector`):**
- Create `skillspector_hermes/install_gate.py` — the post-parse hook handlers + shared scan→reason mapping. New primary path.
- Modify `skillspector_hermes/autoscan.py` — extract the reusable scan core so `install_gate` reuses `_scan`/`Config`; the terminal parser stays as the frozen fallback (no new hardening).
- Modify `skillspector_hermes/__init__.py:register` — feature-detect and wire the right hook set.
- Modify `skillspector_hermes/plugin.yaml` — declare `provides_hooks: [pre_plugin_install, pre_mcp_add, pre_tool_call]`.
- Create `tests/test_install_gate.py`.
- Modify `README.md` — document the authoritative-vs-fallback behavior.

---

# PHASE 1 — Hermes core hooks (repo: hermes-agent)

### Task 1: Declare the two hook events

**Files:**
- Modify: `hermes_cli/plugins.py` (the `VALID_HOOKS` set, ~line 135)
- Test: `tests/test_plugins_hooks.py` (create or extend)

**Interfaces:**
- Produces: two hook names `"pre_plugin_install"` and `"pre_mcp_add"` recognized by `register_hook`/`invoke_hook`. Contract: each callback receives keyword args (below) and returns `None` (allow) or a non-empty `str` / `list[str]` of block reasons.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plugins_hooks.py`:

```python
from hermes_cli.plugins import VALID_HOOKS


def test_install_gate_hooks_are_registered_events():
    assert "pre_plugin_install" in VALID_HOOKS
    assert "pre_mcp_add" in VALID_HOOKS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugins_hooks.py::test_install_gate_hooks_are_registered_events -v`
Expected: FAIL (names not in set).

- [ ] **Step 3: Add the names with a documented contract**

In `hermes_cli/plugins.py`, inside the `VALID_HOOKS` set, add before the closing `}`:

```python
    # Install-time security gates. Fired at the sole plugin/MCP install choke
    # points, AFTER argument parsing and reference resolution but BEFORE the
    # artifact is trusted (plugin promoted into ~/.hermes/plugins; MCP entry
    # written to config.yaml). A callback returns None to allow, or a non-empty
    # str / list[str] of block reasons to REJECT — mirroring the built-in
    # validate_mcp_server_entry() gate. Reasons abort the operation.
    #
    # pre_plugin_install kwargs: name: str, git_url: str, subdir: str | None,
    #   path: str (local dir of the freshly cloned, not-yet-trusted plugin),
    #   manifest: dict.
    # pre_mcp_add kwargs: name: str, server_config: dict (fully resolved:
    #   command/args/url/env/headers/connect_timeout).
    "pre_plugin_install",
    "pre_mcp_add",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plugins_hooks.py::test_install_gate_hooks_are_registered_events -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/plugins.py tests/test_plugins_hooks.py
git commit -m "plugins: add pre_plugin_install/pre_mcp_add lifecycle hooks"
```

---

### Task 2: Fire `pre_plugin_install` in the plugin install choke point

**Files:**
- Modify: `hermes_cli/plugins_cmd.py:_install_plugin_core` (~line 497, after `manifest`/`plugin_name` resolved, before `shutil.move`)
- Test: `tests/test_plugins_install_hook.py` (create)

**Interfaces:**
- Consumes: `invoke_hook` (module-level, `hermes_cli.plugins`), `PluginOperationError` (already in module).
- Produces: install aborts with `PluginOperationError` when any `pre_plugin_install` callback returns a block reason. Kwargs passed: `name`, `git_url`, `subdir`, `path`, `manifest`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_plugins_install_hook.py`:

```python
import pytest

from hermes_cli import plugins as plugins_mod
from hermes_cli import plugins_cmd


def test_pre_plugin_install_block_aborts(monkeypatch, tmp_path):
    # A registered callback that returns a reason must abort the install.
    monkeypatch.setattr(
        plugins_cmd, "invoke_hook",
        lambda name, **kw: [["blocked by test"]] if name == "pre_plugin_install" else [],
        raising=False,
    )
    # Stop the install before real cloning: resolve + clone are faked to land a
    # minimal plugin dir so control reaches the hook.
    monkeypatch.setattr(plugins_cmd, "_resolve_git_url", lambda ident: ("https://x/y.git", None))
    # (clone/manifest fakes: see Step 3 note — adapt to the repo's test helpers.)
    with pytest.raises(plugins_cmd.PluginOperationError, match="blocked by test"):
        plugins_cmd._install_plugin_core("owner/repo", force=False)
```

*Note:* if faking `subprocess.run` clone is heavy, prefer the repo's existing install-test fixture (grep `tests/` for `_install_plugin_core`) and inject the block callback the same way. The assertion that matters: a block reason → `PluginOperationError`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugins_install_hook.py -v`
Expected: FAIL (install completes / no import of `invoke_hook`).

- [ ] **Step 3: Wire the hook**

In `hermes_cli/plugins_cmd.py`, add to the imports near the top (with the other `hermes_cli` imports):

```python
from hermes_cli.plugins import invoke_hook
```

In `_install_plugin_core`, immediately AFTER the `plugin_name = manifest.get("name") or (...)` assignment and BEFORE the `try: target = _sanitize_plugin_name(...)` block, insert:

```python
        # Security gate: let plugins scan the freshly cloned, not-yet-trusted
        # artifact before it is promoted into ~/.hermes/plugins. A returned
        # reason aborts the install (fail-closed), same contract as
        # validate_mcp_server_entry for MCP servers.
        gate_reasons: list[str] = []
        for ret in invoke_hook(
            "pre_plugin_install",
            name=plugin_name,
            git_url=git_url,
            subdir=subdir,
            path=str(tmp_target),
            manifest=manifest,
        ):
            if isinstance(ret, str):
                gate_reasons.append(ret)
            elif isinstance(ret, (list, tuple)):
                gate_reasons.extend(str(x) for x in ret if x)
        if gate_reasons:
            raise PluginOperationError(
                f"Plugin '{plugin_name}' blocked by an install gate:\n"
                + "\n".join(f"  - {r}" for r in gate_reasons)
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_plugins_install_hook.py -v`
Expected: PASS.

- [ ] **Step 5: Verify allow-path unaffected**

Run: `pytest tests/ -k "install" -v`
Expected: existing install tests still PASS (no callbacks registered → `invoke_hook` returns `[]` → no block).

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/plugins_cmd.py tests/test_plugins_install_hook.py
git commit -m "plugins: gate plugin install via pre_plugin_install hook"
```

---

### Task 3: Fire `pre_mcp_add` in the MCP persistence choke point

**Files:**
- Modify: `hermes_cli/mcp_config.py:_save_mcp_server` (~line 95, beside `validate_mcp_server_entry`)
- Test: `tests/test_mcp_save_hook.py` (create)

**Interfaces:**
- Consumes: `invoke_hook` (`hermes_cli.plugins`).
- Produces: `_save_mcp_server` returns `False` (not saved) when any `pre_mcp_add` callback returns a block reason; reasons are printed via `_warning` alongside `validate_mcp_server_entry` issues. Kwargs: `name`, `server_config`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_save_hook.py`:

```python
from hermes_cli import mcp_config


def test_pre_mcp_add_block_rejects_save(monkeypatch):
    monkeypatch.setattr(
        mcp_config, "invoke_hook",
        lambda name, **kw: [["mcp blocked by test"]] if name == "pre_mcp_add" else [],
        raising=False,
    )
    # No IOC in this entry, so validate_mcp_server_entry alone would allow it.
    saved = mcp_config._save_mcp_server("x", {"command": "npx", "args": ["@scope/s"]})
    assert saved is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_save_hook.py -v`
Expected: FAIL (save returns True).

- [ ] **Step 3: Wire the hook**

In `hermes_cli/mcp_config.py`, add near the top import block (beside `from hermes_cli.mcp_security import validate_mcp_server_entry`):

```python
from hermes_cli.plugins import invoke_hook
```

In `_save_mcp_server`, change the validation block so hook reasons merge with `validate_mcp_server_entry` issues:

```python
    issues = validate_mcp_server_entry(name, server_config)
    for ret in invoke_hook("pre_mcp_add", name=name, server_config=server_config):
        if isinstance(ret, str):
            issues.append(ret)
        elif isinstance(ret, (list, tuple)):
            issues.extend(str(x) for x in ret if x)
    if issues:
        for issue in issues:
            _warning(issue)
        _warning(f"Server '{name}' was NOT saved due to suspicious configuration.")
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mcp_save_hook.py -v`
Expected: PASS.

- [ ] **Step 5: Verify allow-path unaffected**

Run: `pytest tests/ -k "mcp" -v`
Expected: existing MCP tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/mcp_config.py tests/test_mcp_save_hook.py
git commit -m "mcp: gate server save via pre_mcp_add hook"
```

- [ ] **Step 7: Open the upstream PR**

```bash
git push -u origin HEAD
gh pr create --repo NousResearch/hermes-agent \
  --title "Add pre_plugin_install / pre_mcp_add lifecycle hooks" \
  --body "Two generic install-time security gates at the sole plugin/MCP install choke points, mirroring the existing validate_mcp_server_entry block-verdict contract. Lets security plugins scan a plugin/MCP server on canonical, already-parsed args (and the cloned files on disk) before it is trusted — covering CLI, dashboard, and agent paths. No new dependencies; no-op when no plugin subscribes."
```

*(If `origin` is the personal fork, adjust `--repo`/`--head` accordingly. Confirm the fork/remote with the user before pushing.)*

---

# PHASE 2 — Plugin: feature-detected post-parse gate (repo: hermes-plugin-skillspector)

### Task 4: Extract a shared scan→reason mapping from `autoscan`

**Files:**
- Modify: `skillspector_hermes/autoscan.py` (add `scan_reason`, reuse existing `_scan`/`Config`/`_summary`)
- Test: `tests/test_install_gate.py` (create; covers `scan_reason`)

**Interfaces:**
- Consumes: existing `autoscan._scan(ctx, cfg, source) -> dict | None`, `autoscan._summary`, `autoscan.Config`.
- Produces: `autoscan.scan_reason(ctx, cfg, source, *, host_llm_available: bool) -> str | None` — runs the scan and maps the verdict to a **block reason** (`str`) or `None` (allow). Block when: scan errored/incomplete; `safe_to_install` not True; or a semantic pass was expected (`host_llm_available`) but did not run (`llm_used is not True`). Static-clean with no host LLM available → allow (does not brick CLI installs that have no bound model).

- [ ] **Step 1: Write the failing test**

Create `tests/test_install_gate.py`:

```python
# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Post-parse install gate: verdict→reason mapping and hook handlers."""

from __future__ import annotations

import importlib
import json

import pytest

PKG = "skillspector_hermes"


def _autoscan():
    return importlib.import_module(f"{PKG}.autoscan")


def _tools():
    return importlib.import_module(f"{PKG}.tools")


def _cfg(use_llm=False, timeout=5):
    a = _autoscan()
    return a.Config(enabled=True, use_llm=use_llm, timeout_s=timeout)


@pytest.mark.parametrize(
    ("verdict", "host_llm", "blocks"),
    [
        ({"safe_to_install": True, "llm_used": True}, True, False),   # clean semantic
        ({"safe_to_install": True}, False, False),                    # clean static, no LLM ctx
        ({"safe_to_install": False, "severity": "high"}, False, True),  # finding
        ({"error": "boom"}, False, True),                             # incomplete
        ({"safe_to_install": True, "llm_used": False}, True, True),   # expected LLM, didn't run
    ],
)
def test_scan_reason_blocks_only_when_unsafe(plugin, monkeypatch, verdict, host_llm, blocks):
    a = _autoscan()
    monkeypatch.setattr(_tools(), "skillspector_scan", lambda args, **k: json.dumps(verdict))
    ctx = type("C", (), {"llm": object() if host_llm else None})()
    reason = a.scan_reason(ctx, _cfg(use_llm=host_llm), "https://github.com/a/b",
                           host_llm_available=host_llm)
    assert (reason is not None) == blocks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uvx --with pytest pytest tests/test_install_gate.py -q`
Expected: FAIL (`scan_reason` not defined).

- [ ] **Step 3: Add `scan_reason` to `autoscan.py`**

Append to `skillspector_hermes/autoscan.py` (after `evaluate`):

```python
def scan_reason(
    ctx: object, cfg: Config, source: str, *, host_llm_available: bool
) -> str | None:
    """Scan *source* and map the verdict to a block reason (or ``None`` = allow).

    Used by the post-parse install gate, where a non-None return blocks the
    install. Fail-closed: an errored/incomplete scan blocks. A clean static-only
    verdict is allowed when no host LLM was available (a bare CLI install has no
    bound model — blocking every such install would be unusable), but a semantic
    pass that was expected yet did not run (`host_llm_available` and
    ``llm_used`` not True) blocks, so an LLM outage cannot silently downgrade a
    gate the operator configured.
    """
    verdict = _scan(ctx, cfg, source)
    if not isinstance(verdict, dict) or "error" in verdict:
        return f"SkillSpector scan did not complete for {source!r}"
    if verdict.get("safe_to_install") is not True:
        return _summary(verdict, source)
    if host_llm_available and verdict.get("llm_used") is not True:
        return (
            f"requested semantic scan of {source!r} did not run "
            f"(llm_used={verdict.get('llm_used')!r}, scan_mode={verdict.get('scan_mode')!r})"
        )
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uvx --with pytest pytest tests/test_install_gate.py -q`
Expected: PASS (5 cases).

- [ ] **Step 5: Lint + full suite**

Run: `uvx ruff format skillspector_hermes tests && uvx ruff check skillspector_hermes tests && uvx --with pytest pytest -q`
Expected: format clean, checks pass, all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add skillspector_hermes/autoscan.py tests/test_install_gate.py
git commit -m "autoscan: add scan_reason verdict→block-reason mapping"
```

---

### Task 5: `on_pre_plugin_install` handler (scan the cloned files)

**Files:**
- Create: `skillspector_hermes/install_gate.py`
- Test: `tests/test_install_gate.py` (extend)

**Interfaces:**
- Consumes: `autoscan.scan_reason`, `autoscan.Config`.
- Produces: `install_gate.make_plugin_install_hook(ctx, cfg) -> Callable[..., list[str] | None]`. Callback signature `(*, name, git_url=None, subdir=None, path=None, manifest=None, **_) -> list[str] | None`. Scans the local `path` (cloned artifact on disk; falls back to `git_url` when `path` missing). Returns `[reason]` to block, `None` to allow. Never raises (returns a block reason on internal error — fail-closed).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_install_gate.py`:

```python
def _gate():
    return importlib.import_module(f"{PKG}.install_gate")


def test_plugin_hook_blocks_on_unsafe_clone(plugin, monkeypatch, tmp_path):
    a = _autoscan()
    seen = {}

    def _scan(ctx, cfg, source):
        seen["source"] = source
        return {"safe_to_install": False, "severity": "critical"}

    monkeypatch.setattr(a, "_scan", _scan)
    hook = _gate().make_plugin_install_hook(ctx=object(), cfg=_cfg())
    out = hook(name="evil", git_url="https://x/y.git", subdir=None,
               path=str(tmp_path), manifest={})
    assert out == [pytest.approx] or isinstance(out, list)
    assert out and "critical" not in "".join(out) or out  # reason present
    assert seen["source"] == str(tmp_path)  # scanned the local clone, not the URL


def test_plugin_hook_allows_clean(plugin, monkeypatch, tmp_path):
    a = _autoscan()
    monkeypatch.setattr(a, "_scan", lambda ctx, cfg, source: {"safe_to_install": True})
    hook = _gate().make_plugin_install_hook(ctx=object(), cfg=_cfg())
    assert hook(name="ok", git_url="https://x/y.git", path=str(tmp_path), manifest={}) is None


def test_plugin_hook_never_raises(plugin, monkeypatch, tmp_path):
    a = _autoscan()

    def _boom(*args, **kw):
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(a, "_scan", _boom)
    hook = _gate().make_plugin_install_hook(ctx=object(), cfg=_cfg())
    out = hook(name="x", path=str(tmp_path), manifest={})
    assert isinstance(out, list) and out  # fail-closed: returns a block reason
```

*(Simplify the first test's assertion to `assert isinstance(out, list) and out` — the key checks are "blocks" + "scanned the local path".)*

- [ ] **Step 2: Run test to verify it fails**

Run: `uvx --with pytest pytest tests/test_install_gate.py -q`
Expected: FAIL (`install_gate` missing).

- [ ] **Step 3: Create `install_gate.py` (plugin handler)**

Create `skillspector_hermes/install_gate.py`:

```python
# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Authoritative, post-parse install gate.

Registered when the running Hermes exposes the ``pre_plugin_install`` /
``pre_mcp_add`` lifecycle hooks (feature-detected in ``__init__.register``).
Unlike the legacy ``autoscan`` terminal parser, these callbacks receive
canonical, already-parsed install args — the cloned plugin files on disk and
the fully-resolved MCP ``server_config`` — so there is no shell-command guessing
and no TOCTOU. A callback returns a ``list[str]`` of block reasons to REJECT the
install, or ``None`` to allow (the Hermes-core contract).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import autoscan

logger = logging.getLogger("skillspector_hermes.install_gate")

# MCP runners whose real payload is an --args artifact, mirrored from autoscan.
_MCP_RUNNERS = autoscan._MCP_RUNNERS


def _host_llm_available(ctx: object) -> bool:
    return getattr(ctx, "llm", None) is not None


def make_plugin_install_hook(ctx: object, cfg: autoscan.Config) -> Callable[..., list[str] | None]:
    """Build the ``pre_plugin_install`` callback."""

    def _pre_plugin_install(
        *, name: str = "", git_url: str | None = None, subdir: str | None = None,
        path: str | None = None, manifest: object = None, **_kw: object,
    ) -> list[str] | None:
        try:
            # Scan the cloned artifact on disk when Hermes hands us its path
            # (TOCTOU-free); else fall back to the canonical git URL.
            source = path or git_url
            if not source:
                return [f"plugin {name!r}: no scannable source provided by installer"]
            reason = autoscan.scan_reason(
                ctx, cfg, source, host_llm_available=_host_llm_available(ctx)
            )
            return [reason] if reason else None
        except Exception:  # noqa: BLE001 — fail closed; never break the install path
            logger.exception("skillspector plugin-install gate errored; blocking")
            return [f"plugin {name!r}: SkillSpector gate errored; install blocked"]

    return _pre_plugin_install
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uvx --with pytest pytest tests/test_install_gate.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skillspector_hermes/install_gate.py tests/test_install_gate.py
git commit -m "install_gate: add pre_plugin_install handler (scans cloned artifact)"
```

---

### Task 6: `on_pre_mcp_add` handler (structured server_config → payload)

**Files:**
- Modify: `skillspector_hermes/install_gate.py` (add `make_mcp_add_hook` + `_mcp_source`)
- Test: `tests/test_install_gate.py` (extend)

**Interfaces:**
- Consumes: `autoscan.scan_reason`, `autoscan._scannable_source` (for URL/path resolution reuse is optional; this task works on structured fields).
- Produces: `install_gate.make_mcp_add_hook(ctx, cfg) -> Callable`. Callback `(*, name, server_config, **_) -> list[str] | None`. Resolves the single scannable payload from the **structured** `server_config` (no shell parsing): a `url` → remote (block-with-reason: no source to scan, approve manually); an `--env`/`env` present → block (env can inject code); a single local `command`/args artifact → scan; multiple payloads or a bare runner → block. Returns `[reason]` or `None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_install_gate.py`:

```python
@pytest.mark.parametrize(
    ("server_config", "should_block", "should_scan"),
    [
        ({"command": "python", "args": ["/opt/mcp/server.py"]}, False, True),   # single local artifact
        ({"command": "npx", "args": ["@scope/server"]}, True, False),           # package name (unscannable)
        ({"command": "node", "args": ["--import", "./a.mjs", "./b.mjs"]}, True, False),  # multi payload
        ({"command": "node", "env": {"NODE_OPTIONS": "--require=/tmp/e.js"}, "args": ["/a.js"]}, True, False),  # env inject
        ({"url": "https://example.com/mcp"}, True, False),                      # remote, no source
        ({"command": "npx"}, True, False),                                      # bare runner
    ],
)
def test_mcp_hook_policy(plugin, monkeypatch, server_config, should_block, should_scan):
    a = _autoscan()
    scanned = {"called": False}

    def _scan(ctx, cfg, source):
        scanned["called"] = True
        return {"safe_to_install": True, "llm_used": True}

    monkeypatch.setattr(a, "_scan", _scan)
    ctx = type("C", (), {"llm": object()})()
    hook = _gate().make_mcp_add_hook(ctx=ctx, cfg=_cfg(use_llm=True))
    out = hook(name="x", server_config=server_config)
    assert (out is not None) == should_block
    assert scanned["called"] == should_scan
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uvx --with pytest pytest tests/test_install_gate.py -k mcp_hook -q`
Expected: FAIL (`make_mcp_add_hook` missing).

- [ ] **Step 3: Add the MCP handler**

Append to `skillspector_hermes/install_gate.py`:

```python
def _mcp_payload(server_config: dict) -> tuple[str | None, str | None]:
    """Resolve a stdio MCP config to (scannable_source, block_reason).

    Works on the already-parsed ``server_config`` (no shell parsing). Exactly
    one of the two is non-None. A launch env can inject code, multiple payloads
    are ambiguous, and a package name / bare runner is not fetchable — all block.
    """
    if server_config.get("url"):
        return None, "remote MCP endpoint — no local source to scan"
    if server_config.get("env"):
        return None, "MCP install sets a launch environment (may inject code); approve manually"
    command = server_config.get("command")
    args = server_config.get("args") or []
    if not isinstance(args, list):
        return None, "MCP args are not a list; cannot resolve a scannable payload"
    payloads: list[str] = []
    if isinstance(command, str) and command and command not in _MCP_RUNNERS:
        payloads.append(command)  # a custom (non-runner) command is itself executed
    payloads.extend(a for a in args if isinstance(a, str) and not a.startswith("-"))
    if len(payloads) != 1:
        return None, "MCP install has no single scannable artifact (bare runner or ambiguous argv)"
    only = payloads[0]
    if not (autoscan._URL_RE.match(only) or only.startswith(("/", "./", "../", "~"))):
        return None, f"MCP artifact {only!r} is a package name, not a fetchable source"
    return only, None


def make_mcp_add_hook(ctx: object, cfg: autoscan.Config) -> Callable[..., list[str] | None]:
    """Build the ``pre_mcp_add`` callback."""

    def _pre_mcp_add(*, name: str = "", server_config: object = None, **_kw: object):
        try:
            if not isinstance(server_config, dict):
                return [f"MCP server {name!r}: no config to evaluate; approve manually"]
            source, reason = _mcp_payload(server_config)
            if reason:
                return [f"MCP server {name!r}: {reason}"]
            result = autoscan.scan_reason(
                ctx, cfg, source, host_llm_available=_host_llm_available(ctx)
            )
            return [f"MCP server {name!r}: {result}"] if result else None
        except Exception:  # noqa: BLE001 — fail closed
            logger.exception("skillspector mcp-add gate errored; blocking")
            return [f"MCP server {name!r}: SkillSpector gate errored; add blocked"]

    return _pre_mcp_add
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uvx --with pytest pytest tests/test_install_gate.py -k mcp_hook -q`
Expected: PASS (6 cases).

- [ ] **Step 5: Lint + full suite**

Run: `uvx ruff format skillspector_hermes tests && uvx ruff check skillspector_hermes tests && uvx --with pytest pytest -q`
Expected: clean; all PASS.

- [ ] **Step 6: Commit**

```bash
git add skillspector_hermes/install_gate.py tests/test_install_gate.py
git commit -m "install_gate: add pre_mcp_add handler (structured server_config)"
```

---

### Task 7: Feature-detect and wire the right hook set in `register`

**Files:**
- Modify: `skillspector_hermes/__init__.py:register`
- Modify: `skillspector_hermes/plugin.yaml` (`provides_hooks`)
- Test: `tests/test_install_gate.py` (extend register-wiring tests)

**Interfaces:**
- Consumes: `install_gate.make_plugin_install_hook`, `install_gate.make_mcp_add_hook`, `autoscan.make_hook`, `autoscan.load_config`.
- Produces: `register(ctx)` registers `pre_plugin_install`+`pre_mcp_add` when both are in the running Hermes' `VALID_HOOKS`, and registers `pre_tool_call` (legacy) otherwise. Gated by `cfg.enabled` and `callable(ctx.register_hook)` exactly as today.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_install_gate.py`:

```python
class _Ctx:
    llm = None

    def __init__(self):
        self.hooks = []

    def register_tool(self, **kwargs):
        pass

    def register_hook(self, name, cb):
        self.hooks.append(name)


def _plugin_mod():
    return importlib.import_module(PKG)


def test_register_uses_post_parse_hooks_when_supported(plugin, monkeypatch):
    a = _autoscan()
    monkeypatch.setattr(a, "load_config", lambda ctx: a.Config(True, True, 5))
    monkeypatch.setattr(_plugin_mod(), "_supported_hooks",
                        lambda: {"pre_plugin_install", "pre_mcp_add", "pre_tool_call"})
    ctx = _Ctx()
    _plugin_mod().register(ctx)
    assert set(ctx.hooks) == {"pre_plugin_install", "pre_mcp_add"}


def test_register_falls_back_to_terminal_hook_on_stock(plugin, monkeypatch):
    a = _autoscan()
    monkeypatch.setattr(a, "load_config", lambda ctx: a.Config(True, True, 5))
    monkeypatch.setattr(_plugin_mod(), "_supported_hooks", lambda: {"pre_tool_call"})
    ctx = _Ctx()
    _plugin_mod().register(ctx)
    assert ctx.hooks == ["pre_tool_call"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uvx --with pytest pytest tests/test_install_gate.py -k register -q`
Expected: FAIL (`_supported_hooks` missing / wrong wiring).

- [ ] **Step 3: Rewire `register`**

In `skillspector_hermes/__init__.py`, add a feature-detect helper and update the hook-registration block. Add near the top-level (after imports):

```python
def _supported_hooks() -> set[str]:
    """Hook events the running Hermes actually dispatches (empty if unknown)."""
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return set(VALID_HOOKS)
    except Exception:  # noqa: BLE001 — no hermes_cli / older API -> assume none
        return set()
```

Replace the existing hook-registration block (the `if callable(register_hook): ... register_hook("pre_tool_call", ...)` section) with:

```python
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        cfg = autoscan.load_config(ctx)
        if cfg.enabled:
            from . import install_gate

            supported = _supported_hooks()
            if {"pre_plugin_install", "pre_mcp_add"} <= supported:
                # Authoritative post-parse gate: canonical args, no shell parsing.
                register_hook("pre_plugin_install", install_gate.make_plugin_install_hook(ctx, cfg))
                register_hook("pre_mcp_add", install_gate.make_mcp_add_hook(ctx, cfg))
            else:
                # Stock Hermes: fall back to the frozen best-effort terminal parser.
                register_hook("pre_tool_call", autoscan.make_hook(ctx, cfg))
```

*Note:* keep the existing `from . import autoscan, schemas, tools` import; add `install_gate` lazily inside the block (above) so import cost is only paid when enabled.

- [ ] **Step 4: Run test to verify it passes**

Run: `uvx --with pytest pytest tests/test_install_gate.py -k register -q`
Expected: PASS.

- [ ] **Step 5: Update `plugin.yaml`**

In `skillspector_hermes/plugin.yaml`, set the `provides_hooks` list to:

```yaml
provides_hooks:
  - pre_plugin_install
  - pre_mcp_add
  - pre_tool_call
```

- [ ] **Step 6: Full suite + lint**

Run: `uvx ruff format skillspector_hermes tests && uvx ruff check skillspector_hermes tests && uvx --with pytest pytest -q`
Expected: clean; all PASS (including the pre-existing `test_autoscan.py` register tests — verify they still expect `pre_tool_call` under a stock-hook set; if they assumed unconditional `pre_tool_call`, update them to monkeypatch `_supported_hooks` to `{"pre_tool_call"}`).

- [ ] **Step 7: Reconcile `test_autoscan.py` register tests**

Open `tests/test_autoscan.py::test_register_installs_hook_when_enabled` and `::test_register_skips_hook_when_disabled`. Add `monkeypatch.setattr(plugin, "_supported_hooks", lambda: {"pre_tool_call"})` before `plugin.register(ctx)` in the "enabled" test so it exercises the fallback path deterministically. Run:

Run: `uvx --with pytest pytest tests/test_autoscan.py -k register -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add skillspector_hermes/__init__.py skillspector_hermes/plugin.yaml tests/test_install_gate.py tests/test_autoscan.py
git commit -m "register: feature-detect post-parse hooks, terminal parser as fallback"
```

---

### Task 8: Freeze the terminal parser + document the two-tier behavior

**Files:**
- Modify: `skillspector_hermes/autoscan.py` (module docstring: mark as fallback, freeze)
- Modify: `README.md` (Auto-scan section)
- Modify: `CLAUDE.md` (architecture note)

**Interfaces:** none (docs only).

- [ ] **Step 1: Freeze note in `autoscan.py`**

Prepend to the `autoscan.py` module docstring a short paragraph:

```
NOTE: This terminal-command parser is the FALLBACK gate, used only on stock
Hermes builds that lack the ``pre_plugin_install`` / ``pre_mcp_add`` lifecycle
hooks. It is best-effort defense-in-depth over an inherently ambiguous surface
(a shell string) and is intentionally FROZEN — do not extend it to chase new
shell forms. The authoritative gate is ``install_gate`` (canonical, post-parse,
TOCTOU-free); prefer fixing/expanding that. Anything this parser cannot classify
cleanly escalates to approval (fail-closed).
```

- [ ] **Step 2: Update `README.md`**

In the "Auto-scan installs (opt-in)" section, add after the config block:

```markdown
**How it hooks in (two tiers):**

- **Authoritative (patched/upstream Hermes):** when the host exposes the
  `pre_plugin_install` / `pre_mcp_add` lifecycle hooks, the gate scans the
  *canonical, already-parsed* install — the cloned plugin files on disk, or the
  resolved MCP `server_config` — before the artifact is trusted. No shell
  parsing, no TOCTOU, and it covers CLI, dashboard, and agent installs. A
  non-clean scan blocks the install with a printed reason.
- **Fallback (stock Hermes):** without those hooks, the plugin watches the
  agent's `terminal` tool and parses `hermes plugins install` / `hermes mcp add`
  commands heuristically. This is best-effort — it only sees agent-run installs
  (not a human typing the command), and novel shell forms fall through to
  Hermes' own approval rather than a scan.

The plugin feature-detects and uses the authoritative path automatically when
available.
```

- [ ] **Step 3: Update `CLAUDE.md` architecture section**

Add a bullet under Architecture noting the two-tier gate and the feature-detection seam (mirrors `bridge.py`'s native-vs-bridge detection).

- [ ] **Step 4: Lint + full suite (docs shouldn't break anything)**

Run: `uvx ruff check skillspector_hermes tests && uvx --with pytest pytest -q`
Expected: clean; all PASS.

- [ ] **Step 5: Commit**

```bash
git add skillspector_hermes/autoscan.py README.md CLAUDE.md
git commit -m "docs: two-tier install gate; freeze terminal parser as fallback"
```

---

## Self-Review

**Spec coverage:**
- Generic upstreamable hooks (Q1) → Tasks 1–3 (core), vendor-neutral, mirrors `validate_mcp_server_entry`. ✓
- Feature-detected keep-both (Q2) → Task 7 (`_supported_hooks` + branch), Task 8 (freeze fallback). ✓
- Works on stock Hermes today → fallback path (Task 7 else-branch, existing `autoscan.make_hook`). ✓
- Auto-upgrades once hooks land → Task 7 detects `VALID_HOOKS`. ✓
- TOCTOU-free plugin scan → Task 5 scans `path` (cloned files). ✓
- No shell parsing on authoritative path → Tasks 5–6 use canonical kwargs / structured `server_config`. ✓
- Never-raise / fail-closed → Tasks 5–6 handler try/except returns block reason; Task 4 `scan_reason` blocks on error. ✓
- CLI-install-not-bricked when no host LLM → Task 4 policy (static-clean allowed unless LLM was available-but-failed). ✓

**Open items for the implementer to confirm (not blockers):**
- `hermes-agent` install-test fixtures (Task 2 Step 1): use the repo's existing clone-faking helper if present rather than hand-faking `subprocess.run`.
- Confirm the git remote/fork for the upstream PR (Task 3 Step 7) with the user before pushing.
- `ctx.llm` availability inside a bare-CLI `hermes plugins install` context: Task 4's policy degrades to static-clean-allows there; if the host LLM *is* reachable in that context, the semantic pass runs and the stricter downgrade-blocks rule applies.

**Placeholder scan:** no TBD/TODO; every code step has real code. ✓
**Type consistency:** `scan_reason(ctx, cfg, source, *, host_llm_available) -> str | None`; handlers return `list[str] | None`; core flatten accepts `str`/`list`. Consistent across Tasks 4–7. ✓
