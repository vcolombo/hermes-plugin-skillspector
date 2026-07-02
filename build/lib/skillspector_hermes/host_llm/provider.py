# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0
#
# Portions adapted from SkillSpector (Apache-2.0,
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES).

"""Bridge provider: exposes the Hermes host LLM to stock SkillSpector.

SkillSpector's provider protocols are deliberately duck-typed — its
``has_cli_capability()`` check states that "providers added externally
(outside this package) also qualify". This provider uses exactly that
sanctioned extension surface: it implements the ``AgentCLICapable`` shape
(``is_available()`` + synchronous ``complete()``) backed by the host
``ctx.llm``, so stock ``llm_utils.get_chat_model`` wraps it in the existing
``AgentCLIChatModel`` adapter — including the schema-augmented structured
output path — with no SkillSpector code changes.

Only used on the bridge path (stock SkillSpector). When the installed
SkillSpector has native embedded-provider support, its own host provider is
used instead and this module stays dormant.
"""

from __future__ import annotations

import os

from ._state import get_host_llm

_PURPOSE = "skillspector-scan"


def _result_text(result: object) -> str:
    text = getattr(result, "text", None)
    return text if isinstance(text, str) else str(result)


class BridgeHostProvider:
    """Duck-typed SkillSpector provider backed by the Hermes host LLM.

    Satisfies the ``LLMProvider`` protocol surface (credentials, metadata) plus
    the ``AgentCLICapable`` extension (``is_available``/``complete``). The host
    owns keys, provider resolution, audit, and — unless the operator says
    otherwise via ``SKILLSPECTOR_MODEL`` — model choice. SkillSpector-internal
    model labels are never forwarded to the host: they are meaningless in the
    host's model namespace and trip the host's override trust gate.
    """

    DEFAULT_MODEL = "host"
    SLOT_DEFAULTS: dict[str, str] = {}

    # -- CredentialsProvider ------------------------------------------------

    def resolve_credentials(self) -> tuple[str, str | None] | None:
        return None

    # -- ModelMetadataProvider ----------------------------------------------

    def get_context_length(self, model: str) -> int | None:
        return None

    def get_max_output_tokens(self, model: str) -> int | None:
        return None

    def resolve_model(self, slot: str = "default") -> str:
        user_input = os.environ.get("SKILLSPECTOR_MODEL", "").strip()
        return user_input or self.SLOT_DEFAULTS.get(slot, "") or self.DEFAULT_MODEL

    # -- ChatModelProvider (no native LangChain model — CLI-capable path) ----

    def create_chat_model(self, model, *, max_tokens, timeout=120):  # noqa: ANN001, ANN201
        """No native LangChain model; ``get_chat_model`` uses the CLI-capable path."""
        return None

    # -- AgentCLICapable ------------------------------------------------------

    def is_available(self) -> tuple[bool, str | None]:
        if get_host_llm() is None:
            return False, "No host LLM is bound (not running inside a Hermes plugin call)."
        return True, None

    def complete(self, prompt: str, *, model: str, max_output_tokens: int) -> str:
        """Synchronous completion via the host's ``complete()``.

        ``model`` (a SkillSpector-internal label) is deliberately not forwarded;
        an operator ``SKILLSPECTOR_MODEL`` override is, and a host trust-gate
        rejection of that override degrades to the host default rather than
        failing the analyzer call.
        """
        host_llm = get_host_llm()
        if host_llm is None:
            raise RuntimeError("No host LLM is bound for this scan.")
        override = os.environ.get("SKILLSPECTOR_MODEL", "").strip() or None
        kwargs: dict[str, object] = {"purpose": _PURPOSE}
        messages = [{"role": "user", "content": prompt}]
        if override:
            try:
                result = host_llm.complete(  # type: ignore[attr-defined]
                    messages, model=override, **kwargs
                )
                return _result_text(result)
            except Exception as exc:  # noqa: BLE001 — retry only the trust rejection
                if type(exc).__name__ != "PluginLlmTrustError":
                    raise
        result = host_llm.complete(messages, **kwargs)  # type: ignore[attr-defined]
        return _result_text(result)
