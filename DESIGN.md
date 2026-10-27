# tagteam — cross-agent review hooks

Anything an agent writes (code, design doc, plan) gets reviewed by the *other* agents
before the turn ends. Works the same whether the author is Claude Code, Codex,
Copilot CLI, or Antigravity (`agy`). Review work is sharded across the three
non-author agents rather than broadcast to all of them.

This is the original design (2026-10-25). Packaging, the config shape, reviewer consent and the loop limits
changed later: see [docs/superpowers/specs/2026-10-26-packaging-design.md](docs/superpowers/specs/2026-10-26-packaging-design.md).
Where the two differ, the spec wins.

## 0. Verified environment (2026-10-25)

| Agent | Version | Hook config | Events we need | Headless reviewer call |
|---|---|---|---|---|
| Claude Code | 2.1.310 | `~/.claude/settings.json` | UserPromptSubmit, PostToolUse, Stop, PreToolUse(ExitPlanMode) | `claude -p --output-format json --json-schema … --permission-mode plan --disallowedTools Write Edit NotebookEdit --model sonnet` |
| Codex | 0.164.0 | `~/.codex/hooks.json` (Claude-shaped; **new hooks need `/hooks` approval**) | UserPromptSubmit, PostToolUse, Stop | `codex exec -s read-only -c approval_policy="never" --output-schema f.json -o out.json -` |
| Copilot CLI | 1.0.90 | `~/.copilot/hooks/tagteam.json` (repo `.github/hooks` loads only in trusted dirs) | userPromptSubmitted, postToolUse, agentStop | `copilot -p … -s --no-ask-user --allow-all-tools --deny-tool write --deny-tool shell --usage-output-file u.json` |
| Antigravity | agy 1.4.2 | `~/.gemini/config/hooks.json` (named hook `tagteam`) | PreInvocation (invocationNum 0 = new turn), PostToolUse, Stop | `agy -p … --mode plan --output-format json --json-schema f.json --effort medium` |

### Spike results (Phase 0, 2026-10-25)

All verified with real headless runs; payloads saved in `tests/fixtures/`.

- **Stop blocking works in Claude, Copilot, agy** end to end (agent made the requested follow-up edit).
  Codex block format comes from its published schema (`stop.command.output`); live test pending user approval of hooks.
- **Block formats:** Claude/Codex/Copilot `{"decision":"block","reason"}`; agy `{"decision":"continue","reason"}`.
  Copilot re-submits the reason as a *user prompt*, which fires userPromptSubmitted again, so turn-start ignores prompts starting with `[tagteam]`.
- **Long hooks:** a 280s Stop hook completed in Claude, Copilot, and agy. Blocking mode is viable, no async fallback needed.
- **`TAGTEAM_DEPTH` is inherited** by hook subprocesses in all agents tested, so the recursion guard holds.
- **Copilot also executes Claude-format hooks** from `.claude/settings.json`. The claude adapter detects Copilot/Codex payloads and no-ops.
- **agy runs hooks with cwd = the hooks.json directory**, so the workspace comes from `workspacePaths[0]`.
- **Read-only reviewers:** Claude, Copilot, and agy refused writes under the flags above. **Codex `-s read-only` alone was NOT enough**:
  with the user's `approvals_reviewer = "auto_review"`, apply_patch escalated and was approved. `-c approval_policy="never"` fixes it.
  Backstop: the core snapshots the tree before and after reviews and logs any change.
- **agy headless auto-denies shell commands and then returns an empty response.** The prompt tells it not to run any, with one retry.
  Default effort spent 26k thinking tokens and 200s on a 5-line diff; `--effort medium` keeps it to roughly 50–80s.
- **Codex hook trust** is a per-hook sha256 over a canonical TOML serialization (`codex-rs/hooks/src/engine/discovery.rs::hook_hash`).
  `bypass_hook_trust` is ignored in user and session config. Users approve once via `/hooks`.
- **Copilot quota:** `gh api /copilot_internal/user` gives `premium_interactions.percent_remaining` (student plan: 200/month).
  On this plan, `--model` accepts only the default (`gpt-5.6-luna`, 1 premium request per run).

Orca already owns entries in all four hook configs. The installer appends tagged entries and never edits others (verified byte-identical).

## 1. Architecture

```
 author agent (any of 4)
   │  UserPromptSubmit ──► tagteam hook <agent> turn-start   (snapshot baseline, save prompt)
   │  PostToolUse      ──► tagteam hook <agent> touch        (<1s: append paths to ledger)
   │  Stop             ──► tagteam hook <agent> turn-end     (diff → classify → route → review → verdict)
   │                                   │
   │                       ┌───────────┼────────────┐   parallel, read-only, TAGTEAM_DEPTH=1
   │                       ▼           ▼            ▼
   │                   reviewer A   reviewer B   (reviewer C only on escalation)
   │                       └───────────┬────────────┘
   │◄── block + findings (severity ≥ major), or allow ──┘
```

One program, `tagteam` (Python 3 stdlib, single entry point), plus a thin **adapter**
per agent. The adapter only translates: agent stdin → `HookEvent`, and `Verdict` →
the agent's expected stdout and exit code. All logic lives in the core.

```
~/.tagteam/                   # user data only (override with TAGTEAM_HOME)
  config.toml                 # your overrides: reviewers.use, models, limits, routing
  state/                      # runs.jsonl, per-session state, reservations, runs/<run-id>/, tagteam.log

tagteam repo/                 # the installed package (`tagteam` console script, `uv tool install`)
  tagteam/                    # cli, core, router, runner, sandbox, limits, live, doctor, install, config
  tagteam/harnesses/          # one module per agent (claude codex copilot agy): hooks + headless reviewer
  tagteam/data/               # config.default.toml, rubrics.toml, findings schema, hook fixtures
```

## 2. Invariants (non-negotiable)

1. **No recursion.** Every reviewer call is spawned with `TAGTEAM_DEPTH=1`. Every
   adapter's first line: `if TAGTEAM_DEPTH set: print agent's no-op response; exit 0`.
   A reviewer that triggers hooks must never start a review.
2. **Author never reviews itself.** The router removes the author from the pool.
3. **Reviewers are read-only.** Enforced by each CLI's sandbox or permission flags, not only by the prompt.
4. **Bounded loops.** At most `max_rounds` (default 2) blocking rounds per turn, keyed by
   `session_id + turn_id`. Claude's `stop_hook_active` and agy's built-in continuation
   cap are a second backstop.
5. **Fail open.** If a reviewer crashes, times out, hits quota, or returns malformed output, the author is not blocked.
   The failure is logged and that reviewer goes on cooldown.
6. **Additive install.** Hook entries are appended alongside Orca's and tagged
   `tagteam-hook-form=1` so uninstall can find exactly ours.

## 3. Schemas

### 3.1 `HookEvent`: what an adapter produces

```jsonc
{
  "v": 1,
  "agent": "claude | codex | copilot | agy",
  "phase": "turn-start | touch | turn-end",
  "session_id": "string",          // agent's own id; adapter synthesizes one if absent
  "turn_id": "string",             // set at turn-start, read back from ledger later
  "cwd": "/abs/path",
  "prompt": "string?",             // turn-start only: the user's request (review intent)
  "paths": ["rel/or/abs"],         // touch only: best-effort parse of tool input
  "stop_hook_active": false,       // turn-end only, if the agent reports it
  "raw": {}                        // original payload, kept for debugging
}
```

### 3.2 Change detection

Detection reads the filesystem, not tool names. Codex writes through `apply_patch` and shell, so a tool-name matcher would miss those changes.

- **git repo:** at `turn-start`, snapshot the working tree without touching the user's index:
  `cp "$(git rev-parse --git-path index)" $tmp && GIT_INDEX_FILE=$tmp git add -A && git write-tree`
  → `baseline_tree`. Seeding from the real index means only dirty files are re-hashed, which keeps this cheap in big repos.
  `--git-path` also handles worktrees. At `turn-end` repeat → `current_tree`. Then
  `git diff baseline_tree current_tree` is exactly this turn's change set: shell writes, generated files, and deletions included.
  Round 2 diffs against the round-1 tree, so reviewers see only the fix delta plus their prior findings.
- **plus out-of-repo ledger paths:** plans often live outside the repo (`~/.claude/plans/`, agent
  plan stores). Any ledger path outside the repo root is added to the change set, so plans are never missed.
- **not a git repo** (e.g. `~/Coding` root): union of ledger `paths` + content hashes captured at touch time.
- **plan gate (better than Stop for plans):** Claude has a PreToolUse hook on `ExitPlanMode` that runs
  a synchronous plan review and denies with findings, so the plan is fixed *before* the user
  approves it. Phase 0 finds the equivalent event in Codex, Copilot, and agy. Where none exists, plans are reviewed at Stop.
- Drop files matching `ignore` globs (lockfiles, `dist/`, `*.min.*`, binaries). If the remaining
  change is below `min_changed_lines` (default 5), skip review.

### 3.3 `ReviewRequest`: one per turn, persisted to `runs/<id>/request.json`

```jsonc
{
  "v": 1,
  "run_id": "2026-10-25T14-03-11Z-ab12",
  "author": "codex",
  "repo": "/abs/repo", "session_id": "...", "turn_id": "...", "round": 1,
  "intent": "user prompt for this turn (truncated 4k chars)",
  "artifacts": [
    {
      "path": "docs/auth-design.md",
      "kind": "code | design | plan",
      "status": "added | modified | deleted",
      "lines_changed": 212,
      "risk": "normal | high",          // high if it matches risk_globs (auth/, migrations/, infra/, *.sql, CI)
      "diff": "unified diff (capped)",
      "content_ref": "path in repo"     // reviewer reads full file itself (it has cwd read access)
    }
  ],
  "assignments": [ { "reviewer": "claude", "lens": "design", "artifacts": ["docs/auth-design.md"] } ],
  "deadline_s": 240
}
```

**Classifier** (first match wins, overridable by config):
- `plan`: `tasks/**`, `**/todo.md`, `**/*plan*.md`, `docs/plans/**`, `.claude/plans/**`
- `design`: `**/*design*.md`, `docs/**/*.md`, `**/adr/**`, `RFC*`, `**/*.puml|*.mmd`
- `code`: everything else

### 3.4 `Findings`: every reviewer must return this (enforced by schema flag where the CLI has one, validated otherwise)

```jsonc
{
  "v": 1,
  "reviewer": "claude",
  "lens": "correctness | design | tests",
  "verdict": "approve | changes_requested",
  "findings": [
    {
      "id": "F1",
      "severity": "blocker | major | minor | nit",
      "file": "src/auth.ts", "line": 42,            // line optional for docs
      "claim": "One sentence: what is wrong.",
      "evidence": "Why: the code, quote, or reasoning that proves it.",
      "suggested_fix": "Concrete change.",
      "confidence": 0.0
    }
  ],
  "summary": "≤3 sentences"
}
```

### 3.5 `Verdict`: core → adapter

```jsonc
{ "action": "allow | block", "message": "markdown fed back to the author", "run_id": "..." }
```

`block` when any finding has severity ≥ `block_at` (default `major`) and confidence ≥ 0.6,
and `round < max_rounds`. Findings are deduplicated across reviewers by (file, line±3, claim similarity).
The message says: *"Cross-review by codex+agy. Fix or rebut each item; rebuttals are
passed to the reviewers next round."* Minor and nit findings are written to
`runs/<id>/verdict.json` and summarized in one line without blocking.

### 3.6 Adapter output mapping

| Agent | allow | block |
|---|---|---|
| Claude | `{}` | `{"decision":"block","reason":msg}`; plan gate: `hookSpecificOutput.permissionDecision = "deny"` (not live-tested: ExitPlanMode is unavailable in `claude -p`) |
| Codex | `{}` | `{"decision":"block","reason":msg}` (from Codex's published schema) |
| Copilot | `{}` | `{"decision":"block","reason":msg}`; reason is re-queued as a user message (verified) |
| agy | `{}` | `{"decision":"continue","reason":msg}` (verified) |

## 4. Routing: spreading the load, protecting the primary agents

Claude Code and Codex are the **primary** agents, where the user does their main work. Copilot and agy are
**reviewer-first**. Routing has two inputs: **tier** (a policy preference) and **headroom**
(the share of the agent's usage limit still left, from 0 to 1).

### 4.1 Headroom: where each agent's remaining quota comes from

| Agent | Source | How we read it | Freshness |
|---|---|---|---|
| Claude | ✅ **Verified 2026-10-25.** Status line stdin carries `rate_limits.five_hour` / `seven_day` = `{used_percentage, resets_at}` (seen: 26% / 4%) | Wrap the existing ponytail status line: a tee script writes the JSON to `~/.tagteam/state/quota/claude.json`, then execs ponytail unchanged (tested, then reverted) | Only refreshes while an interactive Claude session is open, so the staleness rule applies |
| Codex | ✅ **Verified.** Rollout JSONL `rate_limits.primary` = `{used_percent, window_minutes: 10080, resets_at}`, updated every turn (seen 8→13% in one session) | Concurrent sessions each write their own file, so take the most recent `rate_limits` event across files modified in the last window, not just the newest file | Latest Codex turn |
| Copilot | ✅ **Verified.** No live quota, but `--usage-output-file` reports `totalPremiumRequestCost` and `totalNanoAiu` per run (seen: 1 premium request, 0.27 AIU) | **Self-metered**: sum `totalPremiumRequestCost` (premium-request plans) or `totalNanoAiu/1e9` (AI-credit plans) against `monthly_budget` | Exact for our own spend. Blind to the user's interactive Copilot use |
| agy | ✅ **Verified: no quota exposed.** `-p --output-format json` returns only per-run token `usage`. `agy models` shows no quota | **Self-metered**: count runs and tokens against `daily_budget`, plus reactive detection | Approximate |

**Reactive backstop (all agents):** if a reviewer's stderr or exit status matches a quota or rate-limit
pattern, set headroom = 0 until `resets_at` (when known) or `cooldown_min`.
**Stale data:** if the live data is older than its window allows (e.g. Claude data older than 5h), fall back to the agent's
self-metered estimate rather than trusting the old number.

`headroom = min(1 − used_5h, 1 − used_weekly)`, combining whichever windows exist.

### 4.2 Config

```toml
[agents.copilot]
tier = "reviewer"
cmd = ["copilot","-p","{prompt}","-s","--no-ask-user","--deny-tool","write","--deny-tool","shell",
       "--usage-output-file","{usage_file}","--model","{model}"]
model = { code = "gpt-5.x", design = "claude-sonnet-x", plan = "claude-sonnet-x" }  # Copilot can run other vendors' models, so diversity costs no primary quota
weight = { code = 0.9, design = 0.8, plan = 0.8 }
quota = { source = "self", monthly_budget_credits = 300 }
[agents.agy]
tier = "reviewer"
cmd = ["agy","-p","--output-format","json","--json-schema","{schema_file}","--mode","plan"]
weight = { code = 0.8, design = 1.0, plan = 1.0 }
quota = { source = "self", daily_budget_runs = 80 }
[agents.claude]
tier = "primary"
cmd = ["claude","-p","--output-format","json","--json-schema","{schema}","--permission-mode","plan","--disallowedTools","Write,Edit,NotebookEdit"]
weight = { code = 1.0, design = 1.0, plan = 1.0 }
quota = { source = "statusline", reserve_floor = 0.5 }   # never review if <50% left in either window
[agents.codex]
tier = "primary"
cmd = ["codex","exec","-s","read-only","--output-schema","{schema_file}","-o","{out_file}","-"]
weight = { code = 1.0, design = 0.7, plan = 0.8 }
quota = { source = "codex_rollout", reserve_floor = 0.5 }

[routing]
reviewers_by = { small = 1, large = 2, high_risk = 3 }
large_lines = 150
design_and_plan_min = 2
primary_use = "overflow"   # overflow | never | balanced
tier_penalty = 4.0         # how strongly to avoid primaries when they are eligible
cooldown_min = 15
```

### 4.3 Algorithm (deterministic, `state/router.json` under file lock)

1. **Eligible pool** = agents − author − on cooldown − `headroom ≤ 0.05`. Primary agents
   are also dropped when `headroom < reserve_floor`.
2. **n** = reviewers needed (size, risk, kind), clamped to `len(pool)`. If the pool is empty → allow, logged as "unreviewed".
3. **Fill reviewer-tier slots first.** Take reviewer-tier agents in order of `score`.
4. **Primaries only fill remaining slots** (`primary_use = "overflow"`). A primary is added only when
   n exceeds the available reviewer-tier agents, which happens only on high-risk runs or when Copilot or agy is down or out of quota.
   Among eligible primaries, pick the one with the most headroom.
5. **score** = `load_24h / (weight[kind] × headroom)` for reviewer-tier agents. Low headroom pushes an agent's score up
   gradually, so load shifts before it hits a quota wall. In `"balanced"` mode, primaries join
   the same ranking with score × `tier_penalty`.
6. Give each reviewer a different lens. Route mixed-kind turns per group. On failure, retry the next eligible agent.

### 4.4 Resulting distribution

| Author | small code (1) | large code / docs (2) | high-risk (3) |
|---|---|---|---|
| Claude | copilot or agy (more headroom) | copilot + agy | copilot + agy + codex *only if codex ≥ 50% left* |
| Codex | copilot or agy | copilot + agy | copilot + agy + claude *only if ≥ 50% left* |
| Copilot | agy | agy + primary with more headroom | agy + claude + codex (subject to floors) |
| agy | copilot | copilot + primary with more headroom | copilot + claude + codex (subject to floors) |

Since the user works mostly in Claude and Codex, nearly all reviews go to Copilot and agy. Primaries only
see high-risk third opinions and fallback work. `tagteam status` shows the actual split per agent.

Worked example, author = Claude, turn touches `plan.md` + 40 lines of code: plan → agy:design +
copilot:correctness. Code is small → whichever of the two has more headroom, e.g. agy:correctness.
Claude and Codex spend nothing.

## 5. Execution and timing

- Reviewers run **in parallel**, each with `timeout = deadline_s` (default 240s). The
  Stop hook's entry timeout is set to `deadline_s + 30` in every agent's config.
  Orca's entries use 5–10s, but ours must be long. Any agent whose maximum hook timeout is
  below `deadline_s + 30` (Phase 0 checks this) automatically uses async delivery as the author.
- Reviewer prompt = lens rubric + kind rubric + `intent` + artifact list + diffs +
  "You may read any file in cwd; you must not modify anything; output only the Findings JSON."
- Round 2 also includes the author's reply (the final assistant message after the block),
  so rebuttals are judged instead of being re-flagged.

**Default delivery: blocking.** The tradeoff is that the user waits about 1–4 minutes at turn end,
but fixes happen in the same turn while the author still has context. The alternative is `delivery = "async"`:
Stop returns immediately, reviews run detached, and findings are injected as context on
the next UserPromptSubmit. That is faster, but the author may have moved on. Both modes use the same code;
async is also the automatic fallback for any agent whose Stop hook can't block.

## 6. Controls

- `TAGTEAM=off` env var, or a `.tagteam-off` file in the repo → hooks no-op.
- `tagteam status` shows recent runs, per-agent load, cooldowns, and spend.
- `tagteam review --last` reruns the last request manually (debug).
- Per-repo overrides: `<repo>/.tagteam.toml` merges over `~/.tagteam/config.toml`.

## 7. Install and uninstall (merge-only)

| Agent | Where we write |
|---|---|
| Claude | append to `hooks.{UserPromptSubmit,PostToolUse,Stop}` arrays in `~/.claude/settings.json` |
| Codex | append to the same arrays in `~/.codex/hooks.json` |
| Copilot | new file `~/.copilot/hooks/tagteam.json` (Orca's `orca.json` untouched) |
| agy | append to `~/.gemini/config/hooks.json` (or ship as an agy plugin with its own `hooks.json`) |

Installer: back up each file (`*.tagteam.bak`), parse JSON, append entries tagged
`tagteam-hook-form=1`, write atomically, and re-parse to verify. `uninstall` removes only tagged entries.

## 8. Open decisions (defaults chosen; change any)

| Decision | Default | Alternative |
|---|---|---|
| Delivery | blocking at Stop, 240s deadline | async, findings on next prompt |
| Install scope | per-user (all repos) with per-repo off switch | per-repo opt-in only |
| Block threshold | `major` | `blocker` only (less friction) |
| Reviewer count | 1 small code / 2 large or docs / 3 high-risk | always 2 |
| Primary agent use (Claude/Codex) | overflow only, 50% reserve floor | `never` / `balanced` |
| Copilot budget | 300 credits/month self-metered | set to your plan |
| Language | Python 3 stdlib (no deps, ~50ms start) | TypeScript on bun |

## 9. Risks

- **Latency and cost**: mitigated by min-change threshold, ignore globs, sharding, and daily caps.
- **Reviewer noise**: severity and confidence gating, lens separation, dedupe, round-2 rebuttals.
- **CLI drift** (flags or payloads change): every agent-specific detail is isolated in config `cmd` plus one adapter,
  and Phase 0 fixtures become regression tests for adapters.
- **Concurrent sessions in one repo**: state is keyed by session_id, and router.json is updated under `fcntl.flock`.
