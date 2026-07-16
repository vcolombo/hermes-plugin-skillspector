# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Hermes Agent plugin exposing one tool, `skillspector_scan`, that scans skills / MCP
servers / repos for security risks using NVIDIA's SkillSpector. The plugin's job is
plumbing: import SkillSpector's `run_scan` core and route its optional LLM semantic pass
through the *host agent's own model* (`ctx.llm`) so the plugin manages no API keys.

## Commands

```bash
ruff check skillspector_hermes tests          # lint
ruff format --check skillspector_hermes tests # format check (CI enforces)
pytest -q                                      # all tests
pytest tests/test_bridge.py::<name> -q         # single test
```

Tests are **hermetic**: `tests/conftest.py` installs a fake `skillspector` module tree in
`sys.modules`, so SkillSpector need not be installed to run them. There is no build step to
test against — CI runs exactly the three commands above.

## Architecture

Flow: `register` (`__init__.py`) wires `schemas.SKILLSPECTOR_SCAN` to a handler →
`tools.skillspector_scan` validates args, binds the host LLM via `bridge.bind`, then calls
SkillSpector's async `run_scan` through `asyncio.run`.

Two things drive most of the design and are easy to break:

**1. The package is named `skillspector_hermes`, never `skillspector`.** A package named
`skillspector` would shadow the installed SkillSpector distribution that `tools.py` imports
at scan time. The `skillspector` *toolset* string in `register` is just a display namespace
and is fine.

**2. `bridge.py` feature-detects two ways to inject the host LLM into SkillSpector:**
- *Native path* — if `skillspector.providers.host` exists, hand the LLM to SkillSpector's
  own ContextVar. No patching.
- *Bridge path* (stock SkillSpector) — expose the vendored `BridgeHostProvider`
  (`host_llm/provider.py`) via SkillSpector's duck-typed CLI-capable surface, plus two
  idempotent monkeypatch *wrappers*: provider selection and the MCP credential gate. Both
  wrappers delegate to the originals whenever no host LLM is bound, so they are inert
  outside a scan and self-disable on SkillSpector versions that don't need them.

**3. The install gate has two tiers, feature-detected via `VALID_HOOKS`** — mirrors
  `bridge.py`'s native-vs-bridge pattern. When the host exposes
  `pre_plugin_install` / `pre_mcp_add` lifecycle hooks, the plugin uses the
  authoritative post-parse gate (`install_gate.py`) for canonical, TOCTOU-free
  scanning. On stock Hermes lacking those hooks, it falls back to the
  terminal-command parser (`autoscan.py`), which is best-effort and intentionally
  frozen — do not extend it. The fallback watcher (`autoscan.make_hook`) is always
  registered; it delegates to the authoritative gate if available, else parses
  the shell string heuristically.

The host LLM is held in a `ContextVar` (`host_llm/_state.py`) — task-local, async-safe. It
is an opaque `object`; this package never imports the Hermes `agent` package and calls the
LLM by duck typing (`host_llm.complete(messages, purpose=...)`).

## Invariants — do not violate

- **`skillspector_scan` never raises.** Every failure path returns a JSON string
  (`{"error": ...}`), including a missing SkillSpector install. Keep the broad `except` in
  `tools.py`; new logic must preserve this contract.
- **SkillSpector-internal model labels are never forwarded to the host.** Model choice
  belongs to the host. Only an operator's `SKILLSPECTOR_MODEL` override is forwarded, and
  only if the host's trust gate allows it — a `PluginLlmTrustError` degrades to the host
  default rather than failing (see `provider.py::complete`).
- Report honesty fields (`llm_requested` / `llm_available` / `llm_used` / `scan_mode`) must
  stay meaningful so a static-only scan is never mistaken for a full semantic one.
