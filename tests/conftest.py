# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Test fixtures: package loading and a hermetic fake SkillSpector.

The repo directory name (``hermes-plugin-skillspector``) is not a valid
Python identifier, so the plugin package is loaded here under the name Hermes
installs it as (``skillspector_hermes``), with submodule search locations so
the package's relative imports work.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "skillspector_hermes"
PKG_NAME = "skillspector_hermes"


def _load_plugin_package() -> types.ModuleType:
    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]
    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PKG_DIR / "__init__.py",
        submodule_search_locations=[str(PKG_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plugin() -> types.ModuleType:
    """The plugin package, loaded as ``skillspector_hermes``."""
    return _load_plugin_package()


class _DefaultProvider:
    """Stands in for whatever provider stock SkillSpector would select."""


@pytest.fixture()
def fake_stock_skillspector(plugin, monkeypatch: pytest.MonkeyPatch):
    """Install a fake *stock* SkillSpector module tree in sys.modules.

    Mimics the surface the bridge touches: ``providers._select_active_provider``
    and ``mcp_server.resolve_provider_credentials`` (the stock gate spelling).
    No ``providers.use_provider`` — so the bridge path activates.
    Bridge patch state is reset so each test starts unpatched.
    """
    default_provider = _DefaultProvider()

    skillspector = types.ModuleType("skillspector")
    providers = types.ModuleType("skillspector.providers")
    providers._select_active_provider = lambda: default_provider
    mcp_server = types.ModuleType("skillspector.mcp_server")
    mcp_server.resolve_provider_credentials = lambda: None
    skillspector.providers = providers
    skillspector.mcp_server = mcp_server

    monkeypatch.setitem(sys.modules, "skillspector", skillspector)
    monkeypatch.setitem(sys.modules, "skillspector.providers", providers)
    monkeypatch.setitem(sys.modules, "skillspector.mcp_server", mcp_server)

    bridge = plugin.bridge if hasattr(plugin, "bridge") else None
    if bridge is None:
        import importlib

        bridge = importlib.import_module(f"{PKG_NAME}.bridge")
    monkeypatch.setattr(bridge, "_patches_installed", False)

    return types.SimpleNamespace(
        skillspector=skillspector,
        providers=providers,
        mcp_server=mcp_server,
        default_provider=default_provider,
        bridge=bridge,
    )
