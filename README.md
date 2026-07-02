# hermes-plugin-skillspector

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that lets the
agent scan AI skills, MCP servers, and repositories for security risks —
**before installing or trusting them** — using
[SkillSpector](https://github.com/NVIDIA/SkillSpector).

The scanner's optional LLM semantic pass runs on **the host agent's own
model** via Hermes's `ctx.llm` plugin API: no separate API keys, no provider
configuration, full host-side audit attribution. Hermes's posture of "use what
the user is using" applies to the scan too.

## Install

**1. Install SkillSpector into the Hermes Python environment — from Git.**

> ⚠️ Do **not** `pip install skillspector`. The PyPI name was
> [squatted with malware](https://osv.dev/vulnerability/MAL-2026-6561)
> (see [NVIDIA/SkillSpector#240](https://github.com/NVIDIA/SkillSpector/issues/240));
> NVIDIA has not published the package there. Install from the source repo:

```bash
uv pip install --python /opt/hermes/.venv/bin/python \
    git+https://github.com/NVIDIA/SkillSpector.git
```

Note for Hermes docker deployments: Hermes pins `openai`/`anthropic` exactly,
while SkillSpector's floors are newer. Install with overrides so the agent's
own SDKs are untouched (SkillSpector's HTTP providers — the only consumers of
those SDKs — are not used by this plugin):

```bash
printf 'openai==2.24.0\nanthropic==0.87.0\n' > /tmp/override.txt
uv pip install --python /opt/hermes/.venv/bin/python \
    --overrides /tmp/override.txt \
    git+https://github.com/NVIDIA/SkillSpector.git
```

**2. Install the plugin** (run as the user Hermes runs as — *not* root):

```bash
hermes plugins install vcolombo/hermes-plugin-skillspector/skillspector_hermes --enable
```

(The plugin package lives in the `skillspector_hermes/` subdirectory so only
the runtime files are copied into `~/.hermes/plugins`.)

Alternatively, install via pip — Hermes discovers the plugin through its
`hermes_agent.plugins` entry point, no plugins-directory copy needed:

```bash
uv pip install --python /opt/hermes/.venv/bin/python \
    git+https://github.com/vcolombo/hermes-plugin-skillspector.git
```

## Use

Ask the agent to scan anything before installing it:

> Scan https://github.com/someone/suspicious-skill with skillspector_scan
> (use_llm true) before we install it.

The verdict includes `risk_score` (0–100), `severity`, `safe_to_install`,
`findings`, and honest accounting (`llm_requested` / `llm_available` /
`llm_used` / `scan_mode`) so a static-only scan is never mistaken for a full
semantic one. Per-call LLM failures, if any, appear in the report metadata.

## How the host-LLM binding works

The handler binds `ctx.llm` for the duration of each scan and hands it to
SkillSpector through a feature-detecting bridge (`bridge.py`):

- **Native path** — SkillSpector versions with embedded-provider support
  (`skillspector.providers.host`) receive the host LLM through their own
  ContextVar. No patching; native structured output.
- **Bridge path** — stock SkillSpector gets the vendored
  `BridgeHostProvider`, exposed through SkillSpector's sanctioned duck-typed
  CLI-capability surface, plus two idempotent wrapper patches (provider
  selection and the MCP availability gate) that delegate to the originals
  whenever no host LLM is bound. The patches self-disable on SkillSpector
  versions that no longer need them.

SkillSpector-internal model labels are never forwarded to the host — model
choice belongs to the host unless the operator sets `SKILLSPECTOR_MODEL`
*and* allows it in the trust gate:

```yaml
# Hermes config.yaml — all overrides are denied unless the operator opts in.
plugins:
  entries:
    skillspector_hermes:
      llm:
        allow_model_override: true
        allowed_models: [anthropic/claude-3-5-haiku]
```

## Security notes

- The plugin manages **no credentials**; the host performs every LLM call and
  attributes it (`purpose: skillspector-scan`) in its audit trail.
- Scan prompts embed **untrusted skill content**. `ctx.llm` is a bare,
  governed completion API — the right primitive. Do not substitute a
  tool-enabled agent CLI as the scan backend.
- The handler never raises: every failure returns a JSON `{"error": ...}`.

## License

Apache-2.0. Portions adapted from
[SkillSpector](https://github.com/NVIDIA/SkillSpector) (Apache-2.0,
Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES).
