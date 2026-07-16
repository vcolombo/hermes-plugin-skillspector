# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""Authoritative, post-parse install gate.

Registered when the running Hermes exposes the ``pre_plugin_install`` /
``pre_mcp_add`` lifecycle hooks (feature-detected in ``__init__.register``).
Unlike the legacy ``autoscan`` terminal parser, these callbacks receive
canonical, already-parsed install args — the cloned plugin files on disk and
the fully-resolved MCP ``server_config`` — so there is no shell-command guessing.
A callback returns a ``list[str]`` of block reasons to REJECT the install, or
``None`` to allow (the Hermes-core contract).

TOCTOU scope differs by kind:

* **Plugin install is TOCTOU-free** — ``pre_plugin_install`` hands us the cloned
  files on disk and Hermes promotes *those exact bytes* only after a clean
  verdict.
* **MCP add is install-time best-effort, NOT TOCTOU-free** — we scan the artifact
  *reference* (a ``--command``/``--args`` path, or a URL) but Hermes stores that
  reference and launches it later; a swapped file, retargeted symlink, or
  refetched URL can change the launched bytes. Closing this needs core
  launch-time binding (quarantine-copy + launch-from, or digest-pin + revalidate
  before each launch; reject mutable remote URLs). We still fail closed on
  everything we *can* see (loader options, ambiguous/relative artifacts, env
  injection), but a mutable absolute reference is only vetted at install time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from . import autoscan

logger = logging.getLogger("skillspector_hermes.install_gate")

# MCP runners whose real payload is an --args artifact, mirrored from autoscan.
_MCP_RUNNERS = autoscan._MCP_RUNNERS


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
            reason = autoscan.scan_reason(ctx, cfg, source)
            return [reason] if reason else None
        except Exception:  # noqa: BLE001 — fail closed; never break the install path
            logger.exception("skillspector plugin-install gate errored; blocking")
            return [f"plugin {name!r}: SkillSpector gate errored; install blocked"]

    return _pre_plugin_install


def _mcp_payload(server_config: dict) -> tuple[str | None, str | None]:
    """Resolve a stdio MCP config to (scannable_source, block_reason).

    Works on the already-parsed ``server_config`` (no shell parsing). Exactly one
    of the two is non-None. Everything that can't be pinned to a single fetchable
    artifact blocks (fail closed): a launch env or a loader/require/import option
    can inject unscanned code; multiple payloads are ambiguous; a package name or
    bare runner is not fetchable; and a *relative* artifact cannot be bound to the
    directory a later Hermes launch will resolve it against, so only an absolute
    path (or a ``~`` / URL) is scanned.
    """
    if server_config.get("url"):
        return None, "remote MCP endpoint — no local source to scan"
    if server_config.get("env"):
        return None, "MCP install sets a launch environment (may inject code); approve manually"
    command = server_config.get("command")
    args = server_config.get("args") or []
    if not isinstance(args, list):
        return None, "MCP args are not a list; cannot resolve a scannable payload"
    if autoscan.has_code_injecting_option(args):
        return None, "MCP args carry a loader/require/import option that runs unscanned code"
    payloads: list[str] = []
    if isinstance(command, str) and command and command not in _MCP_RUNNERS:
        payloads.append(command)  # a custom (non-runner) command is itself executed
    payloads.extend(a for a in args if isinstance(a, str) and not a.startswith("-"))
    if len(payloads) != 1:
        return None, "MCP install has no single scannable artifact (bare runner or ambiguous argv)"
    only = payloads[0]
    if autoscan._URL_RE.match(only):
        return only, None
    if only.startswith("~"):
        return os.path.expanduser(only), None
    if os.path.isabs(only):
        return only, None
    if only.startswith(("./", "../")):
        return None, (
            f"MCP artifact {only!r} is a relative path; its execution directory is "
            "unbound at install time (a later launch could resolve to a different file)"
        )
    return None, f"MCP artifact {only!r} is not an absolute path or URL (package name or relative)"


def make_mcp_add_hook(ctx: object, cfg: autoscan.Config) -> Callable[..., list[str] | None]:
    """Build the ``pre_mcp_add`` callback."""

    def _pre_mcp_add(*, name: str = "", server_config: object = None, **_kw: object):
        try:
            if not isinstance(server_config, dict):
                return [f"MCP server {name!r}: no config to evaluate; approve manually"]
            source, reason = _mcp_payload(server_config)
            if reason:
                return [f"MCP server {name!r}: {reason}"]
            result = autoscan.scan_reason(ctx, cfg, source)
            return [f"MCP server {name!r}: {result}"] if result else None
        except Exception:  # noqa: BLE001 — fail closed
            logger.exception("skillspector mcp-add gate errored; blocking")
            return [f"MCP server {name!r}: SkillSpector gate errored; add blocked"]

    return _pre_mcp_add
