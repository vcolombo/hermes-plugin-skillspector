# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Handler contract: validation, never-raise, host binding + cleanup."""

from __future__ import annotations

import importlib
import json

import pytest

PKG_NAME = "skillspector_hermes"


def _tools():
    return importlib.import_module(f"{PKG_NAME}.tools")


def _host_llm_pkg():
    return importlib.import_module(f"{PKG_NAME}.host_llm")


class _FakeHostLlm:
    pass


# -- Validation / never-raise (no skillspector needed) -------------------------


@pytest.mark.parametrize("bad_args", [None, [], "target", 42])
def test_non_dict_args_returns_json_error_never_raises(plugin, bad_args):
    out = json.loads(_tools().skillspector_scan(bad_args))
    assert out == {"error": "`args` must be an object."}


def test_missing_target_returns_json_error(plugin):
    out = json.loads(_tools().skillspector_scan({}))
    assert "error" in out and "target" in out["error"]


def test_invalid_output_format_returns_json_error(plugin):
    out = json.loads(_tools().skillspector_scan({"target": "x", "output_format": "xml"}))
    assert "error" in out and "output_format" in out["error"]


def test_non_bool_use_llm_is_rejected_not_coerced(plugin):
    out = json.loads(_tools().skillspector_scan({"target": "x", "use_llm": "false"}))
    assert out == {"error": "`use_llm` must be a boolean."}


def test_non_string_yara_rules_dir_returns_json_error(plugin):
    out = json.loads(_tools().skillspector_scan({"target": "x", "yara_rules_dir": 5}))
    assert "error" in out and "yara_rules_dir" in out["error"]


def test_missing_skillspector_reported_with_git_install_hint(plugin, monkeypatch):
    tools = _tools()

    def _boom(*a, **k):
        raise ModuleNotFoundError("No module named 'skillspector'", name="skillspector")

    monkeypatch.setattr(tools, "_run_scan_sync", _boom)
    out = json.loads(tools.skillspector_scan({"target": "x"}))
    assert "not installed" in out["error"]
    assert "git+https" in out["error"]  # never `pip install skillspector` (PyPI squatted)


# -- Host binding through the bridge -------------------------------------------


def test_host_llm_bound_during_scan_and_reset_after(fake_stock_skillspector, monkeypatch):
    tools = _tools()
    host_llm = _host_llm_pkg()
    seen: dict[str, object] = {}
    host = _FakeHostLlm()

    def _fake_run(target, *, use_llm, output_format, yara_rules_dir):
        seen["bound"] = host_llm.get_host_llm()
        return {"risk_score": 0, "safe_to_install": True}

    monkeypatch.setattr(tools, "_run_scan_sync", _fake_run)
    out = json.loads(tools.skillspector_scan({"target": "x", "use_llm": True}, host_llm=host))
    assert seen["bound"] is host
    assert host_llm.get_host_llm() is None
    assert out["safe_to_install"] is True


def test_host_llm_reset_even_when_scan_raises(fake_stock_skillspector, monkeypatch):
    tools = _tools()

    def _boom(*a, **k):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(tools, "_run_scan_sync", _boom)
    out = json.loads(tools.skillspector_scan({"target": "x"}, host_llm=_FakeHostLlm()))
    assert _host_llm_pkg().get_host_llm() is None
    assert out == {"error": "scan blew up", "type": "RuntimeError"}


def test_register_tolerates_ctx_without_llm(plugin, monkeypatch):
    registered: dict[str, object] = {}

    class _Ctx:
        def register_tool(self, **kwargs):
            registered.update(kwargs)

    plugin.register(_Ctx())
    assert registered["name"] == "skillspector_scan"
    assert callable(registered["handler"])

    tools = _tools()
    seen: dict[str, object] = {}

    def _fake_run(target, *, use_llm, output_format, yara_rules_dir):
        seen["bound"] = _host_llm_pkg().get_host_llm()
        return {"risk_score": 0}

    monkeypatch.setattr(tools, "_run_scan_sync", _fake_run)
    out = json.loads(registered["handler"]({"target": "x"}))
    assert out == {"risk_score": 0}
    assert seen["bound"] is None  # no llm on ctx -> host-less scan, no binding
