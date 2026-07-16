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

## Auto-scan installs (opt-in)

By default the agent decides when to call `skillspector_scan`. You can instead
have the plugin **gate extension installs automatically**: a `pre_tool_call`
hook watches the agent's `terminal` tool, and when it runs `hermes plugins
install`/`update` or `hermes mcp add`/`install`, it scans the target first and —
**fail-closed** — escalates any non-clean or failed scan to Hermes' human
approval prompt. Off unless you enable it:

```yaml
# Hermes config.yaml
plugins:
  entries:
    skillspector_hermes:
      auto_scan:
        enabled: true        # default false
        use_llm: true        # run the semantic pass during the gate (default true)
        scan_timeout_s: 120  # on timeout, fail closed → approval
```

**How it hooks in (two tiers):**

- **Authoritative (patched/upstream Hermes):** when the host exposes the
  `pre_plugin_install` / `pre_mcp_add` lifecycle hooks, the gate scans the
  *canonical, already-parsed* install — the cloned plugin files on disk, or the
  resolved MCP `server_config` — before the artifact is trusted, and covers CLI,
  dashboard, and agent installs. A non-clean scan blocks with a printed reason.
  TOCTOU scope differs by kind:
  - **Plugin install is TOCTOU-free** — Hermes hands the gate the *already
    cloned* files on disk and only promotes those exact bytes into
    `~/.hermes/plugins` after a clean verdict.
  - **MCP add is best-effort, not airtight** — two inherent gaps of vetting a
    *config* instead of sandboxing execution:
    - *Mutable reference (TOCTOU):* the gate scans the artifact reference
      (`--command`/`--args` path, or URL); Hermes stores it and launches it
      later, so a swapped file, retargeted symlink, or refetched URL can change
      the launched bytes.
    - *Un-modeled runner argv:* it fails closed on the option shapes it
      *recognizes* (`--require`/`--import`/`--loader`/`-c`/`-e`/`-r` and attached
      forms, `--env`, and ambiguous/relative/multi artifacts), but does not fully
      model every interpreter's argv — an unrecognized loader/config option (e.g.
      node `--env-file=` carrying `NODE_OPTIONS`, deno `--config`, php
      `-d auto_prepend_file=`) could introduce code the single-artifact scan
      never sees. A clean MCP verdict covers the *recognized* payload only.

    Airtight MCP vetting must happen at the execution boundary (Hermes core:
    quarantine-copy + launch-from, or digest-pin + revalidate before each
    launch). Until then, treat the MCP verdict as install-time, best-effort
    assurance over the common cases.
- **Fallback (stock Hermes):** without those hooks, the plugin watches the
  agent's `terminal` tool and parses `hermes plugins install` / `hermes mcp add`
  commands heuristically. This is best-effort — it only sees agent-run installs
  (not a human typing the command), and novel shell forms fall through to
  Hermes' own approval rather than a scan.

The plugin feature-detects and uses the authoritative path automatically when
available.

**This is best-effort defense-in-depth, not an airtight gate.** Know its limits
before relying on it:

- **TOCTOU** — the hook scans a *mutable reference* (`owner/repo`, `pkg@latest`)
  before Hermes independently fetches it to install; the installed bytes can
  differ from the scanned ones. Airtight scanning must happen on the fetched
  artifact (staged/quarantined), which is what Hermes' own Skills Hub already
  does — so **skills are out of scope here** (already guarded by `skills_guard`).
  This hook covers plugins and MCP servers, where there is no such guard.
- It matches a **single plain `hermes …` command**; anything wrapped or compound
  (`&&`, `sh -c`, variables, pipes) escalates to approval rather than being
  parsed. Installs that bypass the CLI entirely are outside the gate.
- **Remote HTTP MCP** endpoints have no source to scan — they get
  config-validation + approval, not a scan verdict.
- `--yolo`, cached session/always approvals, and cron/direct-dispatch modes can
  skip the human prompt.

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
