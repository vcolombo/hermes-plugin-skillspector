# SPDX-FileCopyrightText: Copyright (c) 2026 Vincent Colombo
# SPDX-License-Identifier: Apache-2.0

"""SkillSpector Hermes Agent plugin registration entry point.

Registers the ``skillspector_scan`` tool under the ``skillspector`` toolset so
a Hermes agent can vet a skill, MCP server, or repository and gate installs on
the returned risk verdict. The optional semantic pass runs on the host's own
model via ``ctx.llm`` — the plugin manages no API keys.

The package is named ``skillspector_hermes`` (via ``plugin.yaml``) rather than
``skillspector`` on purpose: Hermes imports a plugin by its directory name,
and a package named ``skillspector`` would shadow the installed
``skillspector`` distribution that :mod:`tools` imports at scan time, breaking
every scan. The ``skillspector`` *toolset* below is just a display namespace
and does not collide.
"""

from . import autoscan, schemas, tools


def register(ctx):
    """Called once at plugin startup. Wires the scan schema to its handler.

    ``ctx.llm`` is read at call time (not registration time) so each scan uses
    the host's currently-active model, then bound into the handler. A context
    without ``llm`` (stub contexts, alternate runtimes) degrades to host-less
    static-only scanning instead of breaking the handler's never-raise contract.

    When the operator opts in (``auto_scan.enabled`` in this plugin's config) and
    the context supports lifecycle hooks, a ``pre_tool_call`` gate is also
    registered — see :mod:`autoscan`. It is off by default and best-effort
    (defense-in-depth, not airtight; TOCTOU applies).
    """

    def _handler(args, **kwargs):
        return tools.skillspector_scan(args, host_llm=getattr(ctx, "llm", None), **kwargs)

    ctx.register_tool(
        name="skillspector_scan",
        toolset="skillspector",
        schema=schemas.SKILLSPECTOR_SCAN,
        handler=_handler,
    )

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        cfg = autoscan.load_config(ctx)
        if cfg.enabled:
            register_hook("pre_tool_call", autoscan.make_hook(ctx, cfg))
