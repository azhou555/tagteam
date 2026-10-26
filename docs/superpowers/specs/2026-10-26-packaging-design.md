# tagteam for teams: packaging, more harnesses, configurable reviews

Status: ready for approval (2026-10-26)

## Intent

tagteam works today for one person: Alec, with four coding agents (Claude Code, Codex, Copilot CLI,
Antigravity) on personal and student plans. Its routing exists to stretch those plans' usage limits.

The next users are developers at a company, Alec included at work. Their company pays for the agents, usually
through business seats or API-priced accounts. They want the same thing tagteam does now: when an agent
finishes a change, a different agent reviews it, and real problems go back to the author before the turn ends.
The aim is better code at a reasonable cost, without anyone having to remember to ask for a review.

The main worry on company money is runaway review loops with no hard stop. Alec doesn't want a dollar budget
or price cap. He wants guarantees that reviews can't feed each other without end.

## Users and setup

- Developers at a company. Mostly Claude Code and Codex, plus some Kiro, Antigravity, Cursor, OpenCode, and
  possibly Pi.
- Agents are installed and logged in with company accounts. Reviews run through those installed agent apps
  (CLIs), not direct calls to model providers.
- macOS and Linux.

## Goals

1. **Anyone can install it.** One install command, no clone-and-edit. `tagteam install` finds the agents present
   and wires up only those.
2. **Configurable, in plain terms.** A person can say:
   - which agents' work gets reviewed (authors)
   - which kinds of change trigger a review (code, plans, design docs)
   - which agents review, in what order of preference (reviewers)
   - which model each reviewer runs
3. **Explicit reviewer consent.** Code goes only to reviewer agents the user confirmed (see "Reviewer
   consent"). No list, no reviews.
4. **No runaway loops.** Every path that can trigger a review has a hard stop, enforced in code (see
   "Loop safety").
5. **Works on company accounts.** Reviewers are chosen from config, not from usage-limit data that API-priced
   accounts don't expose. Quota-aware routing stays available for personal plans.
6. **More harnesses.** Claude Code and Codex are first-class. Kiro, Cursor, and OpenCode are added as authors and,
   where they have a headless mode, reviewers. Antigravity and Copilot keep working. Pi is a stretch goal.
7. **Spend is visible, not capped.** `status`, `watch`, and the menu bar show reviews this week and, where an agent
   reports it, what they cost.

## Non-goals (MVP)

- Direct model-provider backends (Anthropic, OpenAI, OpenRouter APIs). The reviewer interface must allow
  adding one later without a redesign (see "Reviewer backends").
- Company-internal or private agents.
- Dollar budgets or price caps.
- Windows.
- Central or team-wide policy servers, dashboards, or telemetry. Everything stays on the developer's machine.
- Publishing to PyPI or Homebrew (see "Packaging"; installing from GitHub is enough for now).

## Anti-goals (would be a failure even if it works)

- Any chance of an unbounded review loop, or of reviews re-running on unchanged code.
- An install that silently changes agent config the user didn't agree to, or that breaks an agent's existing
  hooks or status line.
- Blocking a developer's agent because tagteam failed. Hooks must keep failing open, as they do today.
- Sending code to an agent or vendor the user didn't list as a reviewer.
- A config surface so large people can't tell what it does. The defaults must be sensible with zero config.

## Constraints

- Keep the existing behaviour for Alec's personal setup working. Quota routing and the four current agents must
  not regress. The 52 existing tests stay green, rewritten only where the config shape changes.
- Python stdlib only at runtime (as today). Python 3.11+ (`tomllib`).
- Hooks run inside agent turns: every hook must stay fast and fail open.
- Reviewers run read-only. Each reviewer harness must have a verified read-only invocation before it ships.
- Each agent's hook protocol is captured from real payloads (fixtures) before its adapter is written, as in
  Phase 0 of `tasks/todo.md`.

## Decided by Alec

- Target: company-provisioned accounts; Claude Code and Codex first.
- Harnesses to add: Kiro, Antigravity (already done), Cursor, OpenCode; Pi maybe.
- Reviewers run through installed agent apps. No direct model APIs for now, but design for them later.
- Config dimensions: which authors, which triggers, which reviewers, which model.
- Reviewers need an explicit, confirmed list. tagteam must never run every installed agent as a reviewer.
- No price cap; prevent loops instead.
- The above is right for an MVP.

## Design

### 1. Harness registry

Today agent knowledge is spread over `adapters.py` (hook parsing and encoding), `runner.py` (`command()`
and `parse_output()`), `install.py` (config file locations and shapes), and `quota.py`. Each harness becomes one
module under `tagteam/harnesses/<name>.py` that declares what it can do:

```python
class Harness:
    name: str                       # "claude", "codex", "cursor", ...
    def detect(self) -> bool        # installed on this machine?
    # author role (optional): hooks into this agent
    author: AuthorAdapter | None    # install/uninstall hooks, parse payloads, encode block/notice
    # reviewer role (optional): runs this agent headless, read-only
    reviewer: ReviewerBackend | None
```

- `AuthorAdapter` holds the current `parse_*`/`encode` logic and the install/uninstall logic for that agent's
  config file. It declares its capabilities:
  - `can_block_at_stop`: the agent continues the turn when told to
  - `can_notice`: it can show a message to the user, like `systemMessage`
  - `has_plan_gate`
- `ReviewerBackend` is in the next section.
- Adding a harness means one module plus captured fixtures. The four existing agents move into this shape first,
  with no behaviour change, before any new harness is added.

How a harness that can't block degrades:

- **It can't block but can show a notice:** findings are advisory. They appear as a notice, and in `watch`
  and the menu bar, and the turn ends normally. They are not carried into later turns: the next turn has a new
  baseline, and its reviewers wouldn't have the earlier diff to judge them against.
- **It can neither block nor show a notice:** the harness doesn't get the author role. `install` and `doctor`
  say so. It can still be a reviewer.

### 2. Reviewer backends

```python
class ReviewerBackend:
    name: str
    reads_repo: bool                # True for agent CLIs; False for a future direct-API backend
    def available(self) -> bool     # installed and logged in (cheap check, cached)
    def run(self, prompt, cwd, deadline, options, run_dir) -> (meta, result)   # options = [reviewers.<name>] table
```

- The `CliReviewer` base class holds today's `run_one` behaviour: spawn, retry, timeout, JSON extraction,
  and the quota-error cooldown. Each harness supplies its argv builder and its output parser.
- **Read-only, enforced two ways:**
  1. Each harness's argv uses that CLI's own read-only or deny-write flags (as today).
  2. The reviewer runs in a disposable checkout, not the user's repo:
     - `git commit-tree` turns the turn's snapshot tree into a detached commit.
     - `git worktree add --detach` checks it out in a temp directory, which is removed after the run.
     - Writes there can't reach the real working tree.

  The existing postcondition stays: the real tree is snapshotted before and after the review. If it changed,
  that reviewer is disabled until `tagteam doctor` clears it, and the run is logged as suspect. A non-git
  directory falls back to the flags plus the postcondition. A shipping test spawns a fake reviewer that writes
  a file and asserts the real tree is unchanged.
- `meta` gains optional `usage`: `{input_tokens, output_tokens, cost_usd, premium_requests}`, filled from
  whatever the CLI reports. Unknown fields stay absent.
- A future `ApiReviewer` (`reads_repo=False`) gets diff-only prompts with more context included. Nothing else in
  the pipeline may assume a reviewer can read files.

### 3. Configuration

The config is user-level (`~/.tagteam/config.toml`) with a per-repo override (`<repo>/.tagteam.toml`), as
today. The shape is new and readable:

```toml
[authors]
review = ["claude", "codex"]            # whose turns get reviewed; set at install

[triggers]
kinds = ["code", "plan", "design"]      # which changes trigger a review
min_changed_lines = 5

[review]
per_change = { small = 1, large = 2, high_risk = 2, docs = 2 }   # reviewers per change
large_lines = 150                       # a code change this big counts as "large"

[reviewers]
use = ["codex", "claude"]               # who may see your code, in preference order (see "Reviewer consent")

[reviewers.claude]
model = "sonnet"

[reviewers.codex]
model = "gpt-5-codex"
effort = "medium"

[routing]
mode = "ordered"                        # "ordered" (default) or "quota" (personal plans)

[limits]                                # loop safety; see below
max_rounds_per_turn = 2
max_reviews_per_hour = 30
```

- `ordered` mode: pick the first N reviewers from `reviewers.use` that are available, excluding the author.
  Quota data isn't needed. A reviewer in cooldown after errors or quota failures is skipped.
- `quota` mode is today's router (tiers, headroom, reserve floors). Alec's current behaviour is
  `mode = "quota"` plus today's agent table, which a migration writes for existing installs (see Packaging).
- Precedence: the repo file may set `[triggers]`, `[authors]`, `[limits]`, and the review thresholds. It may
  **not** set `[reviewers]` or models, because which vendors see your code is a personal or company choice that
  a cloned repo shouldn't change.
- `tagteam config` prints the merged effective config and where each value came from.

**Where today's keys go** (my call). Old-shape files keep loading: a shim maps the old keys and logs one
deprecation line.

| Today | New |
|---|---|
| `[review].max_rounds` | `[limits].max_rounds_per_turn` |
| `[review].min_changed_lines` | `[triggers].min_changed_lines` |
| `[review].deadline_s`, `block_at`, `min_confidence`, `max_diff_chars`, `ignore` | stay in `[review]` (the "review thresholds") |
| `[classify].*` | unchanged |
| `[routing].small`, `large`, `high_risk`, `docs` | `[review].per_change` |
| `[routing].large_lines` | `[review].large_lines` |
| `[routing].cooldown_min` | `[limits].cooldown_min` |
| `[routing].primary_use`, `tier_penalty`, `unknown_headroom` | `[routing.quota]` (quota mode only) |
| `[agents.<a>].tier`, `weight`, `quota` | `[routing.quota.agents.<a>]` (quota mode only) |
| `[agents.<a>].model`, `effort` | `[reviewers.<a>].model`, `effort` |
| `[agents.<a>].enabled` | **not mapped at load.** Only used to preselect choices in the `install` consent prompt |

**Quota mode with the new keys** (my call):
- `reviewers.use` filters the pool, so an agent that isn't listed never reviews in either mode.
- `[review].per_change` sets how many reviewers a change gets.
- `authors.review` applies.
- Only the choice and order *within* the filtered pool comes from tiers, headroom, and floors.
- `[reviewers.<a>].model` is the only model key.

**Repo-file enforcement:**
- The repo file may set only `[triggers]`, `[authors]`, `[limits]`, `[review]`, and `[classify]`.
- `load_config` filters the repo file to that allowlist *before* merging. Any other table or key (`[reviewers]`,
  `[routing]`, models, or anything unknown) is dropped, and logged once per repo.
- A test proves a repo file can't change `reviewers.use`, a reviewer model, or `routing.mode`.

**Reviewer consent (decided by Alec: an explicit, confirmed list).** tagteam never sends code to an agent just
because it's installed.

- **Install:** `tagteam install` lists the detected agents that could review, says plainly that each one
  receives your diffs, and asks which may review and in what order. Nothing is preselected. The answer is
  saved as `reviewers.use` in `~/.tagteam/config.toml`.
  - Non-interactively, `--reviewers codex,claude` is the consent.
  - `--yes` without `--reviewers` installs hooks but saves no reviewer list.
- **No saved list means no reviews.** Hooks still run. The turn is allowed with a `not reviewed: no reviewers
  configured; run tagteam install` notice, and `doctor` and the menu bar show the same. This applies when:
  - the user config is missing or was deleted
  - `reviewers.use` is absent or empty
  - only a repo file exists
- **The list is a hard filter in both routing modes.** An agent not in `reviewers.use` is never spawned as a
  reviewer, even as quota-mode overflow.
- **It's editable later:** in the config file, or with `tagteam install --reviewers …` again.
- **Only the user can set it:** `[reviewers]` comes only from the user's own config, never from a repo
  (enforced above).
- **Tests:**
  - a missing config, an empty `use`, and a repo-only config each produce zero reviewer spawns
  - an unlisted agent is never spawned in either mode
  - `--yes` alone saves no list
- **Migration for Alec's existing install:** loading an old-shape config never creates `reviewers.use`. Until
  `install` records a confirmed list, existing installs do no reviews and show the `no reviewers configured`
  notice. Running the new `install` shows the four agents tagteam uses today, preselected from the old
  `enabled` flags, and asks for confirmation. A test checks that an old config with `enabled = true` agents and
  no `reviewers.use` spawns no reviewer.
  - The cost: Alec's reviews pause between upgrading and re-running `install`. The upgrade notes say so.

### 4. Loop safety (hard stops)

Each guard is enforced in code, has a test, and logs when it fires. None depends on a price.

1. **No nesting** (exists): reviewer processes carry `TAGTEAM_DEPTH`, and hooks inside them do nothing.
2. **Rounds per turn** (exists): at most `max_rounds_per_turn` blocking rounds. If the last of them blocked, the
   next Stop gets one **confirm review** that can't block (§5a). After that the turn is always allowed. Worst
   case per turn is `max_rounds_per_turn + 1` runs, and the confirm review counts toward the hourly ceiling.
3. **Global hourly ceiling** (new): at most `max_reviews_per_hour` review runs across all sessions and agents.
   A user's own turns aren't capped, since a person in the loop isn't a runaway. This limit is for agents
   looping on their own, and for many parallel sessions. When it fires, the status line, completion notice,
   and menu bar say `review limit reached`.
4. **No re-review of an approved diff** (new): each run records a digest of the artifact diffs it reviewed and
   its verdict. If a trigger's digest matches an *approved* one, it isn't reviewed again. If it matches a
   *blocked* one, the same block is re-issued without spending a review. That counts as a round, so guard 2
   still ends it.
5. **One review at a time per session** (new): a per-session lock. A second trigger while a review runs is
   skipped, not queued.
6. **Failure breaker** (extends today's cooldown): a reviewer that errors twice in a row is paused for
   `cooldown_min`. If every listed reviewer is paused, the turn is allowed with a `not reviewed` notice.

**What counts:**
- A *review run* is one triggered review: one run id, however many reviewers it fans out to.
- Plan-gate reviews count.
- Failed, timed-out, and crashed runs count, because their reservation is spent.
- A re-issued block (guard 4) doesn't count.
- Only a run that finished with a verdict records a digest, so a failed run never suppresses a retry.

**How the limits are enforced:** counters live in the existing state directory (`~/.tagteam/state`) under the
existing `fcntl` lock (`state.locked`).

- **Reserve before spawning:** under that lock, the hook prunes timestamps older than an hour, checks the
  count, and appends a reservation (run id, session, timestamp). It does this before any reviewer process
  starts. Parallel sessions serialize on the lock, so they can't both take the last slot.
- **Crashes still count:** a reservation counts whether or not the run finishes, so a crashed or killed hook
  can't free capacity it used.
- **Failing open never means reviewing anyway:** if the lock or the state file can't be read or written, the
  hook skips the review and allows the turn with a `not reviewed` notice. That way a broken state store can
  never push past the ceiling.
- **Session lock:** the per-session lock reuses the active-run `flock` from `live.tracking`, so a dead holder
  frees it automatically.

The default of 30 an hour is my call and can be tuned during the "week of use" phase.

### 5. Spend visibility

- `runs.jsonl` records each reviewer's `usage`, where reported.
- `status` and `watch` show reviews this week per reviewer, plus summed cost where known
  ("~$1.84 reported; 3 reviews didn't report cost").
- The menu bar dropdown shows the same weekly line. No caps, no warnings.

### 5a. Issue follow-through (requested by Alec)

When reviewers request changes, the status line, completion notice, and menu bar keep showing that until a
later review settles it, so the user can tell whether the issues were actually resolved:

| Moment | Shown |
|---|---|
| A round blocks | `✗ Changes requested: N issues to fix (round 1/2)`; later rounds say `N issues still open after fixes` |
| A re-review after a block approves | `✓ Issues resolved: all N fixed and re-checked` |
| The confirm review still finds issues | `⚠ Unresolved: N issues still open after 2 fix rounds` (turn allowed) |
| The follow-up review failed or found no reviewer | the usual failed/not-reviewed text plus `· N earlier issues not re-checked` |
| The author reverted everything it changed | `✓ Issues resolved: changes reverted` (no review spent) |

To make that possible:
- The author answering with a rebuttal and no file changes is re-reviewed (today it skips review, so the block
  stays unconfirmed). The reviewers get the turn's whole diff since the user's prompt, the prior findings,
  and the reply.
- The confirm review from guard 2 re-checks the last round's issues and can't block.
- The plan gate tracks its own prior findings the same way and passes them to its round 2.

### 6. Packaging and install

My call (Alec didn't specify):

- Make it a proper Python package (`pyproject.toml`), with rubrics, schema, and default config shipped as
  package data. The console entry point is `tagteam`.
- Install: `uv tool install git+https://github.com/azhou555/tagteam` (or `pipx install git+…`). No PyPI for now.
- Everything tagteam writes into other tools calls the installed `tagteam` executable by absolute path, never
  a repo checkout, `PYTHONPATH`, or a specific `python3.13`:
  - agent hooks: `tagteam hook <agent> <phase>`
  - the Claude status line wrapper: `tagteam statusline`
  - the SwiftBar plugin: `tagteam swiftbar`
  - the Spotlight launcher

  The `bin/` scripts remain only for development.
- **Finding the executable:** `install` uses `shutil.which("tagteam")`, falling back to the resolved
  `sys.argv[0]`. `uv tool` and `pipx` keep a stable shim path across upgrades (`~/.local/bin/tagteam`), so
  upgrades don't need a reinstall. `doctor` flags hooks whose path no longer exists.
- **Bundled files:** the default config, rubrics, and schema load via `importlib.resources` rather than
  `state.ROOT`.
- **Development:** Alec's checkout becomes an editable install (`uv tool install -e .`), so the live hooks run
  the working copy.
- **Migrating old hooks:** every entry tagteam ever wrote carries the `tagteam-hook-form=1` tag. `install`
  rewrites tagged entries to the new command whatever path they point to, so old checkout-style commands
  (`<python> <repo>/bin/tagteam hook …`) are found by the tag, not by their path.
- **Checking before rewriting, per agent:** `tagteam hook <agent> <phase> --check` reads a payload on stdin,
  parses it with that agent's adapter, and encodes a sample block and notice. It runs no handler and writes no
  state. Unlike real hooks it fails closed: non-zero exit on any error. For each agent being installed or
  migrated, `install` pipes that agent's bundled fixture through `--check` using the exact new command line.
  It rewrites that agent's entries only if the check passes. Otherwise it leaves the old entries in place
  and reports which agent failed and why.
- `tagteam install`:
  - detects harnesses
  - shows what it will change, per agent
  - asks once (`--yes` skips the prompt)
  - asks which detected agents to review (authors) and which may review (reviewers; see "Reviewer consent"),
    and saves both to `~/.tagteam/config.toml`
  - keeps today's guarantees: merge-only, tagged entries, backups, idempotent
  - `--authors` / `--reviewers` flags limit the scope
- Existing installs migrate: if hooks point to a repo checkout, `install` rewrites them to the entry point. If
  the user config has no `[routing].mode`, it writes `mode = "quota"` with the current agent table, so Alec's
  personal setup keeps routing by quota.
- `tagteam doctor`: for each harness, checks
  - installed
  - logged in (a cheap probe)
  - hooks present and current
  - one-time approval still needed (Codex `/hooks`)
  - reviewer can run read-only (opt-in live probe: `doctor --probe` runs a tiny review)

  It prints fixes in plain words.
- macOS extras stay optional: the status line (Claude only), SwiftBar menu bar, and Spotlight launcher install
  only when present or asked for (`--menubar`). On Linux they're skipped silently.
- README is rewritten for a new user: install, the first review, what the colors mean, configuration, limits,
  uninstall.

### 7. New harnesses

For each of Kiro, Cursor, OpenCode, and Pi, the builder first runs a spike (like Phase 0) and records answers in
`DESIGN.md`:

| Question | Why it matters |
|---|---|
| Hook or extension events for turn start, file edit, turn end? | Author role possible? |
| Can turn-end output make the agent continue? | `can_block_at_stop` vs notice-only |
| Can it show a user-visible message? | completion notice |
| Headless CLI with read-only mode and structured or JSON output? | Reviewer role possible? |
| Model selection flag? | `[reviewers.<name>].model` |
| Reports token/cost usage? | spend visibility |

A harness ships with whichever roles its spike proves, and fixtures from real payloads. Order: Cursor, OpenCode,
Kiro, then Pi (a stretch goal, as an extension if Pi has no hooks).

## Phases

1. **Package and harness registry:** `pyproject`, entry point, move the four agents into
   `harnesses/`, absolute-entry-point hooks, migration. No behaviour change; all tests green.
2. **Config and ordered routing:** new config shape, `ordered` mode as default for new installs, `tagteam
   config`, repo-override precedence.
3. **Loop safety:** guards 3–6 and the reservation protocol, with tests, plus their status-line, notice, and menu bar text.
4. **Doctor and spend:** `tagteam doctor`, usage capture for Claude, Codex, and Copilot, weekly totals in the
   views.
5. **New harnesses:** Cursor → OpenCode → Kiro → (Pi), each spike then adapter then fixtures.
6. **README and a clean-machine install test** (a fresh macOS user account, or a Linux container for the
   non-macOS path).

Each phase ends green and is usable on its own.

## Left to the builder

- Exact module layout inside `tagteam/harnesses/`, class vs function style (match the existing code: small
  functions, few classes).
- How to probe "logged in" cheaply for each CLI.
- Exact wording of new notices, keeping the outcome model in `live.py`.
