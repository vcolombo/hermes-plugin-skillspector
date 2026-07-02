# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0
#
# Portions adapted from SkillSpector (Apache-2.0,
# Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES).

"""Hermes Agent tool schema for SkillSpector scanning."""

SKILLSPECTOR_SCAN = {
    "name": "skillspector_scan",
    "description": (
        "Scan an AI agent skill, MCP server, or repository for security risks "
        "BEFORE installing or trusting it. Accepts a Git URL, file URL, .zip, "
        ".md file (e.g. SKILL.md), or local directory as `target`. Returns a "
        "verdict with risk_score (0-100), severity, recommendation, "
        "safe_to_install, and findings. The llm_used / scan_mode fields report "
        "whether the optional semantic LLM pass actually ran, so a low score "
        "from a static-only scan is not mistaken for a clean full scan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Path, URL, zip, Git repo, or SKILL.md file to scan.",
            },
            "use_llm": {
                "type": "boolean",
                "description": (
                    "Run the optional LLM semantic pass (uses the Hermes host "
                    "model). Defaults to false (fast static-only scan)."
                ),
            },
            "output_format": {
                "type": "string",
                "enum": ["json", "markdown", "sarif", "terminal"],
                "description": "Format of the embedded `report` string. Defaults to json.",
            },
            "yara_rules_dir": {
                "type": "string",
                "description": "Optional directory of additional YARA rules.",
            },
        },
        "required": ["target"],
    },
}
