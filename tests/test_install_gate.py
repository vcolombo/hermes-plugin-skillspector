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
