# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0
#
# Portions adapted from SkillSpector (Apache-2.0,
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES).

"""Hermes Agent tool handler for SkillSpector scanning.

The handler imports SkillSpector's framework-independent ``run_scan`` core (the
same one its MCP server wraps) so the plugin shares one scan implementation.
SkillSpector must be installed in the Hermes Python environment — from Git, not
PyPI (see README; the PyPI name was squatted).

The optional LLM semantic pass runs against the Hermes host model:
``register`` binds ``ctx.llm`` into the handler, and :mod:`bridge` hands it to
SkillSpector — natively when the installed version supports embedded
providers, otherwise via the vendored bridge. No provider/API-key/env handling
lives here — the host owns credentials.

Per the Hermes handler contract, ``skillspector_scan`` always returns a JSON
string and never raises: failures — including a missing SkillSpector install —
come back as ``{"error": ...}``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

_VALID_FORMATS = ("json", "markdown", "sarif", "terminal")


def _run_scan_sync(
    target: str, *, use_llm: bool, output_format: str, yara_rules_dir: str | None
) -> dict[str, Any]:
    """Invoke the async SkillSpector scan core from a synchronous handler."""
    from skillspector.mcp_server import run_scan

    return asyncio.run(
        run_scan(
            target,
            use_llm=use_llm,
            output_format=output_format,
            yara_rules_dir=yara_rules_dir,
        )
    )


def skillspector_scan(args: object, *, host_llm: object | None = None, **_kwargs: object) -> str:
    """Scan a target for security risks and return a JSON verdict string.

    ``host_llm`` is the Hermes ``ctx.llm``, bound by ``register`` at call time;
    when present it drives the optional LLM pass (``use_llm=true``) with no
    plugin-managed credentials. ``args`` is typed ``object`` because the
    never-raise contract requires defensively accepting any payload.
    """
    if not isinstance(args, dict):
        return json.dumps({"error": "`args` must be an object."})

    target = args.get("target")
    if not target or not isinstance(target, str):
        return json.dumps({"error": "`target` is required and must be a string."})

    output_format = args.get("output_format", "json")
    if output_format not in _VALID_FORMATS:
        return json.dumps(
            {
                "error": (
                    f"`output_format` must be one of {list(_VALID_FORMATS)}, got {output_format!r}."
                )
            }
        )

    use_llm_arg = args.get("use_llm", False)
    if not isinstance(use_llm_arg, bool):
        return json.dumps({"error": "`use_llm` must be a boolean."})
    use_llm = use_llm_arg

    yara_rules_dir = args.get("yara_rules_dir")
    if yara_rules_dir is not None and not isinstance(yara_rules_dir, str):
        return json.dumps({"error": "`yara_rules_dir` must be a string path."})

    cleanup = None
    try:
        if host_llm is not None:
            from . import bridge

            cleanup = bridge.bind(host_llm)

        verdict = _run_scan_sync(
            target,
            use_llm=use_llm,
            output_format=output_format,
            yara_rules_dir=yara_rules_dir,
        )
        if not isinstance(verdict, dict):
            return json.dumps(
                {
                    "error": "SkillSpector returned an unexpected (non-object) scan result.",
                    "type": type(verdict).__name__,
                }
            )
        return json.dumps(verdict, default=str)
    except Exception as exc:  # noqa: BLE001 — contract: never raise, return JSON
        if isinstance(exc, ModuleNotFoundError) and exc.name == "skillspector":
            return json.dumps(
                {
                    "error": (
                        "SkillSpector is not installed in the Hermes environment. "
                        "Install it from Git (NOT PyPI — the name was squatted): "
                        "uv pip install git+https://github.com/NVIDIA/SkillSpector.git"
                    ),
                    "detail": str(exc),
                }
            )
        return json.dumps({"error": str(exc), "type": type(exc).__name__})
    finally:
        if cleanup is not None:
            cleanup()
