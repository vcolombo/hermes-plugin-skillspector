# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Bridge activation: native path, patch behavior, gate spellings."""

from __future__ import annotations

import importlib
import sys
import types

PKG_NAME = "skillspector_hermes"


def _bridge():
    return importlib.import_module(f"{PKG_NAME}.bridge")


def _host_llm_pkg():
    return importlib.import_module(f"{PKG_NAME}.host_llm")


class _FakeHostLlm:
    pass


def test_native_path_used_when_available(plugin, monkeypatch):
    """skillspector.providers.host present -> its ContextVar is used, no patches."""
    calls: list[tuple[str, object]] = []
    native = types.ModuleType("skillspector.providers.host")
    native.set_host_llm = lambda h: calls.append(("set", h)) or "tok"
    native.reset_host_llm = lambda t: calls.append(("reset", t))

    skillspector = types.ModuleType("skillspector")
    providers = types.ModuleType("skillspector.providers")
    providers.host = native
    monkeypatch.setitem(sys.modules, "skillspector", skillspector)
    monkeypatch.setitem(sys.modules, "skillspector.providers", providers)
    monkeypatch.setitem(sys.modules, "skillspector.providers.host", native)

    bridge = _bridge()
    monkeypatch.setattr(bridge, "_patches_installed", False)

    host = _FakeHostLlm()
    cleanup = bridge.bind(host)
    assert calls == [("set", host)]
    assert _host_llm_pkg().get_host_llm() is None  # vendored state untouched
    assert bridge._patches_installed is False  # no patching on the native path
    cleanup()
    assert calls == [("set", host), ("reset", "tok")]


def test_bridge_path_patches_selection_and_gate(fake_stock_skillspector):
    """Stock SkillSpector: bound -> bridge provider + truthy gate; unbound -> originals."""
    fx = fake_stock_skillspector
    bridge = fx.bridge
    host_llm = _host_llm_pkg()

    cleanup = bridge.bind(_FakeHostLlm())
    try:
        selected = fx.providers._select_active_provider()
        assert type(selected).__name__ == "BridgeHostProvider"
        assert fx.mcp_server.resolve_provider_credentials() == ("host", None)
    finally:
        cleanup()

    # After cleanup the wrappers delegate to the originals.
    assert fx.providers._select_active_provider() is fx.default_provider
    assert fx.mcp_server.resolve_provider_credentials() is None
    assert host_llm.get_host_llm() is None


def test_bridge_patches_are_idempotent(fake_stock_skillspector):
    fx = fake_stock_skillspector
    cleanup1 = fx.bridge.bind(_FakeHostLlm())
    cleanup1()
    first_wrapper = fx.providers._select_active_provider
    cleanup2 = fx.bridge.bind(_FakeHostLlm())
    cleanup2()
    assert fx.providers._select_active_provider is first_wrapper  # not re-wrapped


def test_gate_patch_handles_204_spelling(plugin, monkeypatch):
    """A future resolve_chat_model_credentials gate (#204) is patched too."""
    skillspector = types.ModuleType("skillspector")
    providers = types.ModuleType("skillspector.providers")
    providers._select_active_provider = lambda: None
    mcp_server = types.ModuleType("skillspector.mcp_server")
    mcp_server.resolve_chat_model_credentials = lambda: None
    monkeypatch.setitem(sys.modules, "skillspector", skillspector)
    monkeypatch.setitem(sys.modules, "skillspector.providers", providers)
    monkeypatch.setitem(sys.modules, "skillspector.mcp_server", mcp_server)
    monkeypatch.delitem(sys.modules, "skillspector.providers.host", raising=False)

    bridge = _bridge()
    monkeypatch.setattr(bridge, "_patches_installed", False)

    cleanup = bridge.bind(_FakeHostLlm())
    try:
        assert mcp_server.resolve_chat_model_credentials() == ("host", None)
    finally:
        cleanup()
    assert mcp_server.resolve_chat_model_credentials() is None


def test_gate_patch_skips_capability_aware_skillspector(plugin, monkeypatch):
    """No known gate name (capability-aware SkillSpector) -> nothing to patch, no error."""
    skillspector = types.ModuleType("skillspector")
    providers = types.ModuleType("skillspector.providers")
    providers._select_active_provider = lambda: None
    mcp_server = types.ModuleType("skillspector.mcp_server")  # neither gate name
    monkeypatch.setitem(sys.modules, "skillspector", skillspector)
    monkeypatch.setitem(sys.modules, "skillspector.providers", providers)
    monkeypatch.setitem(sys.modules, "skillspector.mcp_server", mcp_server)
    monkeypatch.delitem(sys.modules, "skillspector.providers.host", raising=False)

    bridge = _bridge()
    monkeypatch.setattr(bridge, "_patches_installed", False)

    cleanup = bridge.bind(_FakeHostLlm())  # must not raise
    cleanup()
    assert not hasattr(mcp_server, "resolve_provider_credentials")
