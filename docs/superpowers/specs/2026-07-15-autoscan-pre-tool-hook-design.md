# Auto-scan extension installs via a `pre_tool_call` hook

Date: 2026-07-15
Status: proposed (revised after review by the Hermes agent "Benson"; verdict GO-WITH-CHANGES)

## What this is (and is NOT)

A **best-effort, defense-in-depth gate**: a config-gated `pre_tool_call` hook
that notices when the agent is about to *shell out* to install a Hermes plugin
or MCP server, scans the target with SkillSpector first, and — **fail-closed** —
escalates anything not-clean to the existing human-approval gate.

It is **not** a deterministic or airtight gate. It watches the agent's
`terminal` tool; it does not intercept the installer's actual fetch. See
[Honest limitations](#honest-limitations) — TOCTOU is real and this must be
described as defense-in-depth, never as a guarantee.

## Context

Today the plugin is advisory: it registers one tool (`skillspector_scan`) and
the agent decides when to call it. That both false-triggered on a plain CLI
tool (`yt-dlp`, fixed by narrowing the tool description) and, worse, never
*guarantees* a real extension is scanned before it is trusted. This hook adds a
policy layer that does not depend on the model remembering.

### Decisions (locked with the user)

- **Fail-closed** — an extension install is never auto-allowed unless the scan
  returned a clean verdict.
- **Escalate-to-human-approval** — a bad/failed verdict returns
  `action: "approve"` (human decides), not a silent block. **No hard block in
  v1** (`hard_block_severity` dropped — unused complexity against this decision).
- **Opt-in** — off by default; enabled per-deployment in `config.yaml`.

## Facts verified against a live Hermes deployment (v0.18.2)

1. **Hook contract — confirmed.** A `pre_tool_call` callback may return
   `{"action":"block","message":...}` or
   `{"action":"approve","message":...,"rule_key":...}`; `approve` enters the
   existing once/session/always/deny gate. The callback sees `tool_name ==
   "terminal"` and the effective `args["command"]` **before** execution.
2. **Install surface — confirmed with a correction.** The `terminal` tool
   (required arg `command`) is how the agent installs extensions; there is **no**
   dedicated agent install tool. Correct command grammar:
   ```
   hermes plugins install <ref>
   hermes plugins update  [<ref>]        # ← also fetches new executable code
   hermes mcp add <name> --url|--command|--preset ...
   hermes mcp install <catalog-name>
   ```
   Invalid forms my earlier draft used — `hermes skills add`, and positional
   `hermes mcp add <name> <url>` — do **not** exist. `mcp add` targets live
   behind `--url`/`--command`/`--preset`.
3. **Skills already scanned — confirmed → skip skills in v1.** `hermes skills
   install` downloads into quarantine, runs `tools.skills_guard.scan_skill()`,
   applies policy, then installs those exact bytes. Adding SkillSpector there
   just double-prompts. **v1 covers plugins + MCP only.**
4. **Fail-open is the Hermes core default.** Hermes **catches hook exceptions
   and proceeds**. Therefore fail-closed is achieved *only* by our own callback
   catching every scan/parse/decode error and returning `action:"approve"`. A
   callback that raises = the install silently proceeds unscanned. This makes
   the internal try/except **mandatory**, not defensive polish.

## Architecture

One new isolated module + a config-gated hook registration. No change to the
existing scan path, bridge, or never-raise tool contract.

### New: `skillspector_hermes/autoscan.py`

- `parse_install_target(command: str) -> Target | None`
  Returns the extension ref iff `command` is a **single, plain** Hermes
  extension-install/update invocation:
  - `hermes plugins install <ref>` / `hermes plugins update [<ref>]`
  - `hermes mcp add <name> --command <local...>` (stdio/local: scannable)
  - `hermes mcp add <name> --url <http...>` / `--preset <name>` (remote: **not**
    source-scannable — flag as `kind="remote"`, see policy)
  - `hermes mcp install <catalog-name>`
  **Do not build a shell parser.** If install-words appear but the command also
  contains `&&`, `||`, `;`, newlines, `sh -c`/`bash -lc`, variable expansion,
  or multiple commands → return a sentinel `Target(kind="unparseable")` so the
  policy escalates to approval rather than guessing.

- `evaluate(target, verdict) -> dict | None`
  - `target.kind == "remote"` (HTTP MCP): no source to scan → return an
    **approve** directive citing config-validation, not a fake scan verdict.
  - `target.kind == "unparseable"`: **approve** (fail-closed on ambiguity).
  - clean verdict (`safe_to_install is True`) → `None` (allow).
  - unsafe / `None` / missing fields → **approve** with a risk summary.
  Every approve directive carries a **narrowly scoped `rule_key`** (e.g.
  `skillspector:plugins-install:<ref>`) so an `[a]lways` decision does not
  broaden to unrelated installs.

- `make_hook(ctx, cfg) -> Callable`
  Returns the `pre_tool_call` callback, closed over `ctx` (for `ctx.llm` at call
  time) and the **validated startup `cfg`** (never re-read config per call).
  Callback, wrapped in a total try/except that fails closed:
  1. `tool_name != "terminal"` → `None`. (No `cfg.enabled` check — the hook is
     only registered when enabled.)
  2. `command = args.get("command")`; not a `str` → `None`.
  3. `target = parse_install_target(command)`; `None` → `None`
     (fast reject of every non-extension command, incl. pip/apt/yt-dlp).
  4. Scan by **reusing** the existing handler under a **bounded timeout**:
     `tools.skillspector_scan({"target": target.ref, "use_llm": cfg.use_llm,
     "output_format": "json"}, host_llm=getattr(ctx, "llm", None))`.
  5. Any exception / timeout / decode failure → **log it** and return an
     approve directive (fail-closed events must not be operationally invisible).
  6. else `return evaluate(target, verdict)`.
  Must accept `**kwargs` (hook kwargs evolve) and be safe under **concurrent**
  invocation (parallel tool calls).

### Edit: `skillspector_hermes/__init__.py`

```python
from hermes_cli.config import load_config, cfg_get   # imported lazily in register

def register(ctx):
    ...  # existing tool registration unchanged
    plugin_id = ctx.manifest.key or ctx.manifest.name
    cfg = cfg_get(load_config(), "plugins", "entries", plugin_id,
                  "auto_scan", default={})
    if cfg.get("enabled"):
        ctx.register_hook("pre_tool_call", autoscan.make_hook(ctx, autoscan.Config(cfg)))
```

Config is read once at load; changing it requires a Hermes restart/reload.

### Edit: `skillspector_hermes/plugin.yaml`

Add `provides_hooks: [pre_tool_call]` and document the config block.

### Config (`config.yaml`, opt-in, defaults off)

```yaml
plugins:
  entries:
    skillspector_hermes:
      auto_scan:
        enabled: true            # default false — nothing changes until set
        use_llm: true            # run the semantic pass during the gate
        scan_timeout_s: 120      # bounded; on timeout → fail-closed approve
```

## Honest limitations (must ship in README, not just here)

- **TOCTOU — the core caveat.** The hook scans a *mutable reference*
  (`owner/repo`, `pkg@latest`); Hermes then fetches it **again** to install.
  Scanner may see commit A, installer receive commit B. A real guarantee needs
  scanning the *fetched artifact* (staged install / pinned digest) — which is
  what Hermes' own Skills Hub does and what a `pre_tool_call` hook cannot. This
  hook is defense-in-depth only. Upgrade path: a staged-install hook in Hermes core.
- **Bypasses that skip the human prompt:** `--yolo`, already-cached
  session/always approvals, approved cron/direct-dispatch modes. Document them.
- **Shell-parser bypasses:** anything but a single plain command escalates to
  approval by design, but direct registry/config/file writes that install code
  without the `hermes …` CLI are entirely outside this gate.
- **Remote HTTP MCP** has no source to scan; treated as config-validation +
  approval, never a source-scan claim.

## Files

- add  `skillspector_hermes/autoscan.py`
- edit `skillspector_hermes/__init__.py`  (config-gated hook registration)
- edit `skillspector_hermes/plugin.yaml`  (`provides_hooks`, config docs)
- add  `tests/test_autoscan.py`
- edit `README.md`  (auto-scan opt-in **and** the honest-limitations section)

## Verification

- **Unit (hermetic):** `parse_install_target` accepts the four valid grammars,
  returns `remote`/`unparseable` sentinels correctly, and returns `None` for
  `pip install yt-dlp`, `apt install ripgrep`, `hermes chat`, and compound
  shell. `evaluate`: clean→None; unsafe/None/missing→approve; remote→approve;
  every approve carries a scoped `rule_key`. Config-disabled: hook not
  registered (assert). Exception safety: a scan that raises → approve, not a
  raised exception.
- **Integration:** one test driving the callback the way Hermes does — through
  `handle_function_call()` — not only the pure functions.
- **Lint/format:** `ruff check` + `ruff format --check` (line-length 100).
- **Manual against Benson (VPS Docker Hermes):** enable config, then install a
  suspect plugin → approval prompt; `pip install yt-dlp` → no prompt.

## Resolved open questions

1. **Scope:** plugins + MCP only. Skip skills (already guarded). For MCP, scan
   only local/stdio artifacts; remote HTTP → config-validation + approval.
2. **Block vs approve:** approve-only in v1; `hard_block_severity` removed. Add
   hard-block later only against a concrete policy need. Always pass a scoped
   `rule_key`.
