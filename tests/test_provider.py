# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""BridgeHostProvider: duck-typed CLI-capable surface over the host LLM."""

from __future__ import annotations

import importlib

import pytest

PKG_NAME = "skillspector_hermes"


def _pkg():
    return importlib.import_module(f"{PKG_NAME}.host_llm")


class _Result:
    def __init__(self, text: str) -> None:
        self.text = text


class PluginLlmTrustError(Exception):
    """Same type NAME as the host's trust-gate rejection (matched by name)."""


class _RecordingHostLlm:
    """Records sync complete() calls; optionally rejects model overrides."""

    def __init__(self, *, reject_override: bool = False) -> None:
        self.calls: list[dict] = []
        self._reject = reject_override

    def complete(self, messages, **kwargs):
        if self._reject and kwargs.get("model"):
            raise PluginLlmTrustError("model override not allowed")
        self.calls.append({"messages": messages, **kwargs})
        return _Result("host says hi")


def test_available_only_when_bound(plugin):
    pkg = _pkg()
    provider = pkg.BridgeHostProvider()
    ok, reason = provider.is_available()
    assert ok is False and reason

    token = pkg.set_host_llm(_RecordingHostLlm())
    try:
        ok, reason = provider.is_available()
        assert ok is True and reason is None
    finally:
        pkg.reset_host_llm(token)


def test_protocol_surface(plugin):
    provider = _pkg().BridgeHostProvider()
    assert provider.resolve_credentials() is None
    assert provider.get_context_length("host") is None
    assert provider.get_max_output_tokens("host") is None
    assert provider.resolve_model() == "host"
    assert provider.create_chat_model("host", max_tokens=64) is None  # CLI-capable path


def test_complete_calls_host_without_model_label(plugin, monkeypatch):
    """SkillSpector-internal model labels are never forwarded to the host."""
    monkeypatch.delenv("SKILLSPECTOR_MODEL", raising=False)
    pkg = _pkg()
    host = _RecordingHostLlm()
    token = pkg.set_host_llm(host)
    try:
        text = pkg.BridgeHostProvider().complete(
            "analyze this", model="deepseek-ai/deepseek-v4-flash", max_output_tokens=512
        )
    finally:
        pkg.reset_host_llm(token)
    assert text == "host says hi"
    call = host.calls[0]
    assert call["messages"] == [{"role": "user", "content": "analyze this"}]
    assert call["purpose"] == "skillspector-scan"
    assert "model" not in call


def test_operator_override_forwarded_and_trust_rejection_degrades(plugin, monkeypatch):
    pkg = _pkg()
    monkeypatch.setenv("SKILLSPECTOR_MODEL", "operator-model")

    forwarded = _RecordingHostLlm()
    token = pkg.set_host_llm(forwarded)
    try:
        pkg.BridgeHostProvider().complete("p", model="host", max_output_tokens=64)
    finally:
        pkg.reset_host_llm(token)
    assert forwarded.calls[0]["model"] == "operator-model"

    gated = _RecordingHostLlm(reject_override=True)
    token = pkg.set_host_llm(gated)
    try:
        text = pkg.BridgeHostProvider().complete("p", model="host", max_output_tokens=64)
    finally:
        pkg.reset_host_llm(token)
    assert text == "host says hi"
    assert "model" not in gated.calls[0]  # retried without the override


def test_complete_unbound_raises(plugin):
    with pytest.raises(RuntimeError, match="No host LLM"):
        _pkg().BridgeHostProvider().complete("p", model="host", max_output_tokens=64)
