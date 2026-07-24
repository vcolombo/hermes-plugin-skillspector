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
    ("verdict", "use_llm", "host_llm", "blocks"),
    [
        # (verdict, cfg.use_llm, host_llm_available, expected-block)
        (
            {"safe_to_install": True, "llm_used": True},
            True,
            True,
            False,
        ),  # clean, semantic confirmed
        ({"safe_to_install": True}, False, True, False),  # clean static, semantic not requested
        ({"safe_to_install": False, "severity": "high"}, False, False, True),  # finding
        ({"error": "boom"}, False, False, True),  # incomplete
        # use_llm requested AND a model is bound but the pass didn't run -> block (round 2)
        ({"safe_to_install": True, "llm_used": False}, True, True, True),
        ({"safe_to_install": True}, True, True, True),  # bound model, no llm_used -> block
        # use_llm requested but NO model bound (bare CLI/dashboard) -> clean static allows (round 3)
        ({"safe_to_install": True}, True, False, False),
        ({"safe_to_install": True, "llm_used": False}, True, False, False),
    ],
)
def test_scan_reason_policy(plugin, monkeypatch, verdict, use_llm, host_llm, blocks):
    a = _autoscan()
    monkeypatch.setattr(_tools(), "skillspector_scan", lambda args, **k: json.dumps(verdict))
    reason = a.scan_reason(
        object(), _cfg(use_llm=use_llm), "https://github.com/a/b", host_llm_available=host_llm
    )
    assert (reason is not None) == blocks


def test_scan_reason_no_llm_bound_does_not_hard_reject_default_install(plugin, monkeypatch):
    # Regression: use_llm defaults true, but the bare CLI/dashboard install has no
    # bound model. A clean static verdict must ALLOW rather than block every such
    # install. (A bound-but-unused model is a real downgrade and still blocks.)
    a = _autoscan()
    monkeypatch.setattr(
        _tools(), "skillspector_scan", lambda args, **k: json.dumps({"safe_to_install": True})
    )
    cfg = a.Config(enabled=True, use_llm=True, timeout_s=5)  # default use_llm
    assert a.scan_reason(object(), cfg, "https://github.com/a/b", host_llm_available=False) is None
    assert (
        a.scan_reason(object(), cfg, "https://github.com/a/b", host_llm_available=True) is not None
    )


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
    out = hook(name="evil", git_url="https://x/y.git", subdir=None, path=str(tmp_path), manifest={})
    assert isinstance(out, list) and out
    assert any("critical" in r for r in out)
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


@pytest.mark.parametrize(
    ("server_config", "should_block", "should_scan"),
    [
        (
            {"command": "python", "args": ["/opt/mcp/server.py"]},
            False,
            True,
        ),  # single local artifact
        ({"command": "npx", "args": ["@scope/server"]}, True, False),  # package name (unscannable)
        (
            {"command": "node", "args": ["--import", "./a.mjs", "./b.mjs"]},
            True,
            False,
        ),  # multi payload
        (
            {"command": "node", "env": {"NODE_OPTIONS": "--require=/tmp/e.js"}, "args": ["/a.js"]},
            True,
            False,
        ),  # env inject
        ({"url": "https://example.com/mcp"}, True, False),  # remote, no source
        ({"command": "npx"}, True, False),  # bare runner
        (
            {"command": "node", "args": ["--require=/tmp/evil.js", "/tmp/clean.js"]},
            True,
            False,
        ),  # attached loader option runs unscanned code -> block, never scan (#1)
        (
            {"command": "node", "args": ["-r", "/tmp/evil.js", "/tmp/clean.js"]},
            True,
            False,
        ),  # separate-value loader flag (#1)
        (
            {"command": "node", "args": ["./server.js"]},
            True,
            False,
        ),  # relative artifact -> unbound execution dir -> block (#2)
        (
            {"command": "node", "args": ["server.js"]},
            True,
            False,
        ),  # bare relative artifact -> not absolute -> block (#2)
        (
            {"command": "node", "args": ["~/mcp/server.js"]},
            False,
            True,
        ),  # ~ expands to an absolute path -> scannable
        (
            {"command": "bash", "args": ["-c", "/opt/approved; /opt/evil"]},
            True,
            False,
        ),  # `bash -c '<code>'` runs a command string, not a scannable file -> block (#1)
        (
            {"command": "python", "args": ["-c", "import os; os.system('x')"]},
            True,
            False,
        ),  # `python -c '<code>'` inline code -> block (#1)
        (
            {"command": "python", "args": ["-c__import__('os')", "/tmp/clean.py"]},
            True,
            False,
        ),  # attached `-c<code>` (no space) must not let clean.py vouch for it (#1)
        (
            {"command": "node", "args": ["-r/tmp/evil.js", "/tmp/clean.js"]},
            True,
            False,
        ),  # attached `-r<file>` preload
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


# -- register wiring: feature-detect which hook set to install ------------------


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
    monkeypatch.setattr(
        _plugin_mod(),
        "_supported_hooks",
        lambda: {"pre_plugin_install", "pre_mcp_add", "pre_tool_call"},
    )
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
