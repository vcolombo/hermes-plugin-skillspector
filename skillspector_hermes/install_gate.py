# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Authoritative, post-parse install gate.

Registered when the running Hermes exposes the ``pre_plugin_install`` /
``pre_mcp_add`` lifecycle hooks (feature-detected in ``__init__.register``).
Unlike the legacy ``autoscan`` terminal parser, these callbacks receive
canonical, already-parsed install args — the cloned plugin files on disk and
the fully-resolved MCP ``server_config`` — so there is no shell-command guessing
and no TOCTOU. A callback returns a ``list[str]`` of block reasons to REJECT the
install, or ``None`` to allow (the Hermes-core contract).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from . import autoscan

logger = logging.getLogger("skillspector_hermes.install_gate")

# MCP runners whose real payload is an --args artifact, mirrored from autoscan.
_MCP_RUNNERS = autoscan._MCP_RUNNERS


def _host_llm_available(ctx: object) -> bool:
    return getattr(ctx, "llm", None) is not None


def make_plugin_install_hook(ctx: object, cfg: autoscan.Config) -> Callable[..., list[str] | None]:
    """Build the ``pre_plugin_install`` callback."""

    def _pre_plugin_install(
        *,
        name: str = "",
        git_url: str | None = None,
        subdir: str | None = None,
        path: str | None = None,
        manifest: object = None,
        **_kw: object,
    ) -> list[str] | None:
        try:
            # Scan the cloned artifact on disk when Hermes hands us its path
            # (TOCTOU-free); else fall back to the canonical git URL.
            source = path or git_url
            if not source:
                return [f"plugin {name!r}: no scannable source provided by installer"]
            reason = autoscan.scan_reason(
                ctx, cfg, source, host_llm_available=_host_llm_available(ctx)
            )
            return [reason] if reason else None
        except Exception:  # noqa: BLE001 — fail closed; never break the install path
            logger.exception("skillspector plugin-install gate errored; blocking")
            return [f"plugin {name!r}: SkillSpector gate errored; install blocked"]

    return _pre_plugin_install
