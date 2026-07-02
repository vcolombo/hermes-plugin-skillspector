# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Vendored host-LLM bridge package (used only against stock SkillSpector)."""

from ._state import get_host_llm, reset_host_llm, set_host_llm
from .provider import BridgeHostProvider

__all__ = ["BridgeHostProvider", "get_host_llm", "reset_host_llm", "set_host_llm"]
