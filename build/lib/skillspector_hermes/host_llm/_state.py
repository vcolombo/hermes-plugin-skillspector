# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0
#
# Portions adapted from SkillSpector (Apache-2.0,
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES).

"""Process-wide injection point for the Hermes host LLM.

The plugin handler binds the host ``ctx.llm`` here for the duration of a scan.
When the installed SkillSpector has native embedded-provider support
(``skillspector.providers.host``), this module is unused — the native
ContextVar is used instead (see :mod:`bridge`). A ``ContextVar`` (not a plain
global) keeps the value task-local and async-safe.

The value is an opaque ``object`` — this package never imports the Hermes
``agent`` package; the provider calls its methods by duck typing.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_HOST_LLM: ContextVar[object | None] = ContextVar("hermes_skillspector_host_llm", default=None)


def set_host_llm(host_llm: object) -> Token:
    """Bind *host_llm* for the current context; returns a reset token."""
    return _HOST_LLM.set(host_llm)


def get_host_llm() -> object | None:
    """Return the host LLM bound in the current context, or ``None``."""
    return _HOST_LLM.get()


def reset_host_llm(token: Token) -> None:
    """Undo a prior :func:`set_host_llm`, restoring the previous binding."""
    _HOST_LLM.reset(token)
