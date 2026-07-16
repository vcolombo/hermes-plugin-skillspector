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
        ({"safe_to_install": True, "llm_used": True}, True, False),  # clean semantic
        ({"safe_to_install": True}, False, False),  # clean static, no LLM ctx
        ({"safe_to_install": False, "severity": "high"}, False, True),  # finding
        ({"error": "boom"}, False, True),  # incomplete
        ({"safe_to_install": True, "llm_used": False}, True, True),  # expected LLM, didn't run
    ],
)
def test_scan_reason_blocks_only_when_unsafe(plugin, monkeypatch, verdict, host_llm, blocks):
    a = _autoscan()
    monkeypatch.setattr(_tools(), "skillspector_scan", lambda args, **k: json.dumps(verdict))
    ctx = type("C", (), {"llm": object() if host_llm else None})()
    reason = a.scan_reason(
        ctx, _cfg(use_llm=host_llm), "https://github.com/a/b", host_llm_available=host_llm
    )
    assert (reason is not None) == blocks


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
