# tagteam for Teams: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tagteam installable and safe for company use:
- a packaged CLI with per-harness modules
- an explicit reviewer list, with routing by that list
- hard loop stops and read-only reviewer sandboxes
- `doctor` and spend visibility

**Architecture:**
- Agent-specific code moves into `tagteam/harnesses/<name>.py` behind two small interfaces: author (hooks) and
  reviewer (headless CLI).
- Config gets a new readable shape in `tagteam/config.py`. A legacy shim keeps old files loading, and a
  repo-file allowlist stops a cloned repo from changing reviewers.
- Loop stops live in `tagteam/limits.py`, built on the existing `state.locked` flock.

**Tech Stack:** Python ≥3.11, stdlib only at runtime. `unittest`. Packaged with `pyproject.toml` (hatchling
build backend); installed with `uv tool install` or `pipx`.

**Spec:** `docs/superpowers/specs/2026-10-26-packaging-design.md`

**Out of scope here:** Phase 5 (Cursor, OpenCode, Kiro, Pi). Each needs its spike first (spec §7); write
one short plan per harness after its spike, using the Task 2 interfaces. The spec §1 degrade path is also
deferred to those plans, because all four current authors can block:
- `can_block_at_stop = False` means findings go out as an advisory notice.
- With neither blocking nor notices, the harness gets no author role.

The first harness that lacks blocking implements it.

## Global Constraints

- Runtime is Python stdlib only, `requires-python = ">=3.11"`. macOS and Linux; no Windows.
- Every hook fails open: any exception means allow the turn and log it. The **only** exception is
  `hook --check`, which fails closed (non-zero exit).
- "Fail open" never means "review anyway". On state, lock, or config errors, skip the review and show the
  `not reviewed` notice.
- Code goes only to agents in the user's `reviewers.use`. No list means no reviews. A repo file can never set
  `[reviewers]`, `[routing]`, or models.
- Repo-file allowlist: `[triggers]`, `[authors]`, `[limits]`, `[review]`, `[classify]`.
- Defaults:
  - `max_rounds_per_turn = 2`
  - `max_reviews_per_hour = 30`
  - `cooldown_min = 15`
  - `per_change = { small = 1, large = 2, high_risk = 2, docs = 2 }`
  - `large_lines = 150`
  - `min_changed_lines = 5`
  - `mode = "ordered"`
- Notice text (exact, prefixed with `[tagteam] ` where shown in the transcript):
  - `⚠ Not reviewed: no reviewers configured; run tagteam install`
  - `⚠ Not reviewed: review limit reached (30 per hour)` (with the configured number)
  - `⚠ Not reviewed: no listed reviewer is available`
- Everything tagteam writes into other tools calls the installed `tagteam` executable by absolute path.
- Existing tests keep passing. Rewrite a test only where the config shape it reads has changed, and say so in
  the commit message.
- Test command for the whole suite: `python3 -m unittest discover -s tests -t .`

## Review Focus

1. **Malformed `~/.tagteam/config.toml` (a TOML syntax error):** the hook must not crash the agent. The turn is
   allowed, the `not reviewed` notice shows, and the error is logged. Covered in Task 4,
   `test_bad_user_config_fails_open_without_review`.
2. **Two sessions finishing at the same moment with one slot left under the hourly ceiling:** exactly one
   review runs. Covered in Task 6, `test_parallel_reservations_never_exceed_ceiling`.
3. **`reviewers.use` lists only the author, or only agents that aren't installed:** no spawn, and the
   `no listed reviewer is available` notice. Covered in Task 5, `test_only_author_or_missing_listed`.
4. **Review in a non-git directory:** no worktree is possible, so the reviewer still runs with its CLI flags and
   the postcondition. A write in a non-git directory is detected by the touched-file digest check. Covered in
   Task 7, `test_non_git_dir_falls_back_to_flags_and_postcondition`.
5. **Upgrading with old hooks still installed and no re-install yet:** old-shape config, no `reviewers.use`.
   Hooks keep working and nothing is reviewed. Covered in Task 5,
   `test_legacy_config_without_consent_spawns_nothing`.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` (new) | Package metadata, `tagteam = "tagteam.cli:main"` script, package data |
| `tagteam/data/` (new; moved from repo root) | `config.default.toml`, `rubrics.toml`, `findings.schema.json`, `fixtures/` (one hook payload per harness for `--check`) |
| `tagteam/resources.py` (new) | `text(name) -> str`, `path(name) -> Traversable` via `importlib.resources` |
| `tagteam/harnesses/__init__.py` (new) | Registry: `ALL`, `get(name)`, `detected()` |
| `tagteam/harnesses/base.py` (new) | `Author` and `Reviewer` dataclasses (fields below) |
| `tagteam/harnesses/{claude,codex,copilot,agy}.py` (new) | Each agent's parse, encode, install shape, argv, output parser, usage parser |
| `tagteam/adapters.py` | Keeps `HookEvent`, `extract_paths`, `MARKER`. `parse`/`encode` delegate to the registry |
| `tagteam/runner.py` | Prompt building and `run_all`. `run_one` uses `Reviewer`; runs inside the worktree sandbox (Task 7) |
| `tagteam/config.py` (new) | Load, merge, legacy shim, repo allowlist, provenance; replaces `state.load_config` |
| `tagteam/router.py` | `route()` dispatches `ordered` or `quota`, both filtered by `reviewers.use` |
| `tagteam/limits.py` (new) | Hourly reservation, diff-digest memory, failure breaker, disabled reviewers |
| `tagteam/sandbox.py` (new) | Disposable worktree context manager, plus the write postcondition |
| `tagteam/doctor.py` (new) | Per-harness checks and `--probe` |
| `tagteam/install.py` | Consent prompt, per-agent `--check` before rewrite, entry-point commands |
| `tagteam/live.py` | New notices; weekly spend line; issue follow-through kinds (Task 11) |
| `tests/helpers.py` (new) | Sets `TAGTEAM_HOME` before import; `make_repo`, `fx`, `finding` moved here |
| `tests/test_*.py` (split) | `test_tagteam.py` is split by module as tasks touch it |

---

### Task 1: Package and bundled resources

**Files:**
- Create: `pyproject.toml`, `tagteam/resources.py`, `tests/helpers.py`, `tests/test_package.py`
- Move: `config.default.toml` → `tagteam/data/config.default.toml`; `prompts/rubrics.toml` →
  `tagteam/data/rubrics.toml`; `schema/findings.schema.json` → `tagteam/data/findings.schema.json`
- Modify: `tagteam/state.py` (drop `ROOT`-based file reads), `tagteam/runner.py:14-15` (rubrics/schema via
  resources), `tests/test_tagteam.py` (import shared helpers)

**Interfaces:**
- Produces:
  - `resources.text(name: str) -> str`
  - `resources.path(name: str) -> pathlib.Path`, a real filesystem path, needed because CLIs take
    `--output-schema <file>`. Use `importlib.resources.as_file` and cache it for the process lifetime.
- Produces: the console script `tagteam`

- [ ] **Step 1: Write the failing tests** in `tests/test_package.py`:
  - `test_resources_load_from_any_cwd`: `os.chdir(tempfile.mkdtemp())`, then
    `"review" in tomllib.loads(resources.text("config.default.toml"))`, which holds for both the old and the
    new shape so Task 4 doesn't break it. Also `"preamble" in tomllib.loads(resources.text("rubrics.toml"))` and
    `resources.path("findings.schema.json").is_file()`. Task 4's `test_legacy_keys_map` covers the
    `review.max_rounds` → `limits.max_rounds_per_turn` move.
  - `test_installed_entry_point_runs`: skipped unless `shutil.which("uv")`. Runs
    `uv tool install --force -e . ` into a temp `UV_TOOL_DIR`/`UV_TOOL_BIN_DIR`, then
    `<bin>/tagteam status` exits 0.
- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_package -v`. Expected: FAIL with
  `ModuleNotFoundError: tagteam.resources`.
- [ ] **Step 3: Implement.** In `pyproject.toml`:
  - `[project] name = "tagteam"`, `version = "0.2.0"`, `requires-python = ">=3.11"`, `dependencies = []`,
    `[project.scripts] tagteam = "tagteam.cli:main"`
  - hatchling with `tagteam/data/**` included

  Move the files with `git mv`. Point `state`/`runner` at `resources`.
- [ ] **Step 4: Run the whole suite.** `python3 -m unittest discover -s tests -t .`. Expected: all pass
  (52 + 2).
- [ ] **Step 5: Commit.** `git commit -m "Package tagteam: pyproject, bundled data via importlib.resources"`

### Task 2: Harness registry (no behaviour change)

**Files:**
- Create: `tagteam/harnesses/{__init__,base,claude,codex,copilot,agy}.py`, `tests/test_harnesses.py`,
  `tagteam/data/fixtures/{claude,codex,copilot,agy}-stop.json` (copied from `tests/fixtures`)
- Modify: `tagteam/adapters.py` (move the per-agent code out), `tagteam/runner.py` (`command`,
  `parse_output`, and the agent branches in `run_one` move out), `tagteam/quota.py` (unchanged readers,
  looked up by harness name)

**Interfaces:**
- Produces in `harnesses/base.py`:
  ```python
  @dataclass(frozen=True)
  class Author:
      parse: Callable[[str, dict], HookEvent | None]         # (phase, payload)
      encode: Callable[[str, dict | None], str]              # (phase, verdict) -> stdout
      install: Callable[[str, int, bool], None]              # (entry_cmd, stop_timeout, dry_run)
      uninstall: Callable[[], None]
      can_block_at_stop: bool
      can_notice: bool
      has_plan_gate: bool

  @dataclass(frozen=True)
  class Reviewer:
      argv: Callable[[str, dict, str, int], tuple[list, str | None, str | None]]   # (prompt, options, workdir, deadline)
      parse: Callable[[str, str | None], dict | None]       # (stdout, out_file) -> normalized result
      usage: Callable[[str, str], dict]                     # (stdout, workdir) -> {input_tokens, output_tokens, cost_usd, premium_requests} subset
      reads_repo: bool = True
  ```
- Produces in `harnesses/__init__.py`:
  - `Harness(name: str, detect: Callable[[], bool], author: Author | None, reviewer: Reviewer | None)`
  - `ALL: dict[str, Harness]`
  - `get(name) -> Harness`
  - `detected() -> list[str]`: `shutil.which` on each CLI
- `runner.run_one(agent, cfg, prompt, cwd, deadline, run_dir)` keeps its signature and calls
  `get(agent).reviewer`. `options` is `cfg["reviewers"].get(agent, {})` once Task 4 lands; until then it's
  `cfg["agents"][agent]`.

- [ ] **Step 1: Write the failing tests** in `tests/test_harnesses.py`:
  - `test_registry_roles`: `set(ALL) == {"claude","codex","copilot","agy"}`, every one has `author` and
    `reviewer`, `ALL["claude"].author.has_plan_gate is True`, and the others are `False`.
  - `test_argv_unchanged`: for each agent, `get(a).reviewer.argv("P", {"model": "m"}, "/w", 100)` equals what
    `runner.command` returns today. Capture it into the test as literal lists before deleting `command`.
- [ ] **Step 2: Run them.** `python3 -m unittest tests.test_harnesses -v`. Expected: FAIL with
  `ModuleNotFoundError`.
- [ ] **Step 3: Move the code.** Each `harnesses/<name>.py` gets that agent's `parse_*`, its `encode` branch,
  its `install.py` block (config path and entry shape), its `command` branch, its `parse_output` branch, and
  its usage-file read. `adapters.parse/encode` become one-line registry lookups.
- [ ] **Step 4: Run the whole suite.** All existing adapter, runner, install, and end-to-end tests pass
  unchanged.
- [ ] **Step 5: Commit.** `git commit -m "Move agent-specific code into tagteam/harnesses (no behaviour change)"`

### Task 3: Entry-point commands, `hook --check`, safe migration

**Files:**
- Modify: `tagteam/cli.py` (the `--check` flag; `statusline` and `swiftbar` subcommands), `tagteam/install.py`,
  `tagteam/harnesses/*.py` (install shapes use `entry_cmd`), `bin/tagteam-statusline` (dev only)
- Test: `tests/test_install.py` (move the `Install` class here)

**Interfaces:**
- Consumes: `Harness.author.install/uninstall`, `resources.path("fixtures/<agent>-stop.json")`
- Produces:
  - `install.entry() -> str`: `shutil.which("tagteam")`, else `os.path.realpath(sys.argv[0])`
  - `cli.check(agent, phase) -> int`: 0 or 1, no state writes
  - hook command format `: tagteam-hook-form=1; '<entry>' hook <agent> <phase>`
  - status line command `'<entry>' statusline`
  - SwiftBar plugin body `exec '<entry>' swiftbar`

- [ ] **Step 1: Write the failing tests:**
  - `test_check_mode_fails_closed`: `tagteam hook claude turn-end --check` with stdin `not json` exits 1. With
    the bundled fixture it exits 0 and prints `ok claude turn-end`.
  - `test_check_mode_writes_no_state`: after `--check` on every bundled fixture, `TAGTEAM_HOME/state/sessions`
    is empty.
  - `test_install_rewrites_old_checkout_hooks_by_tag`: a settings file with
    `": tagteam-hook-form=1; '/usr/bin/python3' '/old/repo/bin/tagteam' hook claude turn-end"` becomes one entry
    per event with the new entry path. Non-tagteam entries are byte-identical.
  - `test_failed_check_keeps_old_entries`: with `install.check_agent` patched to return 1 for `codex`, the
    codex file is unchanged, the claude file is rewritten, and the output contains
    `codex: kept existing hooks (check failed`.
- [ ] **Step 2: Run them.** Expected: FAIL (`--check` unknown).
- [ ] **Step 3: Implement.** `install` runs `check_agent(name)` before calling `author.install(...)` for each
  selected agent. `check_agent` pipes the fixture through `[entry, "hook", name, "turn-end", "--check"]`.
- [ ] **Step 4: Run the whole suite.** All pass.
- [ ] **Step 5: Commit.** `git commit -m "Hooks call the installed tagteam; per-agent --check gates migration"`

### Task 4: Config module, legacy shim, repo allowlist, `tagteam config`

**Files:**
- Create: `tagteam/config.py`, `tests/test_config.py`
- Modify: `tagteam/data/config.default.toml` (new shape, spec §3), every `state.load_config` caller
  (`cli.py`, `core.py`, `install.py`), `tagteam/state.py` (remove `load_config`, `_merge`)

**Interfaces:**
- Produces:
  - `config.load(repo: str | None) -> dict`
  - `config.load_with_sources(repo) -> tuple[dict, dict[str, str]]`: dotted key → `"default" | "user" | "repo"`
  - `config.REPO_ALLOWED = {"triggers", "authors", "limits", "review", "classify"}`
  - `config.ConfigError(Exception)`
  - `cli` gains `tagteam config`, which prints `key = value  # source`
- The new default shape has `[authors]`, `[triggers]`, `[review]` (with `per_change` and `large_lines`),
  `[reviewers]` (no `use` key: consent comes only from the user file), `[routing] mode = "ordered"` plus
  `[routing.quota]` with today's agent table, and `[limits]`.

- [ ] **Step 1: Write the failing tests:**
  - `test_legacy_keys_map`: a user file with `[review] max_rounds = 3`, `[routing] small = 2`,
    `large_lines = 99`, `cooldown_min = 7`, `[agents.codex] model = "x"` loads as
    `limits.max_rounds_per_turn == 3`, `review.per_change.small == 2`, `review.large_lines == 99`,
    `limits.cooldown_min == 7`, `reviewers.codex.model == "x"`.
  - `test_legacy_enabled_is_not_consent`: `[agents.codex] enabled = true` leaves `"use" not in cfg["reviewers"]`.
  - `test_repo_file_cannot_set_reviewers_models_or_mode`: a repo file setting `[reviewers] use = ["evil"]`,
    `[reviewers.claude] model = "x"`, `[routing] mode = "quota"`, `[review] block_at = "blocker"` leaves the
    first three at their user/default values, takes `block_at`, and logs `repo config ignored` once.
  - `test_sources`: a key set in the repo file reports `"repo"`.
  - `test_bad_user_config_fails_open_without_review` (Review Focus 1): a user file `[[[` raises `ConfigError`
    from `load`. Through `cli.hook`, a Stop with changes returns `{"systemMessage": "[tagteam] ⚠ Not reviewed:
    config error, see tagteam.log"}` for claude, with zero `run_all` calls.
- [ ] **Step 2: Run them.** Expected: FAIL (`tagteam.config` missing).
- [ ] **Step 3: Implement.** Filter the repo file to `REPO_ALLOWED` *before* merging. The legacy shim runs on
  the user file and the repo file before filtering. In `cli.hook`, catch `ConfigError` and encode the notice.
- [ ] **Step 4: Run the whole suite.** Update the router tests' `CFG` reads to the new keys (`routing.small`
  becomes `review.per_change.small`). All pass.
- [ ] **Step 5: Commit.** `git commit -m "New config shape with legacy shim, repo allowlist, tagteam config"`

### Task 5: Explicit reviewer consent, ordered routing, author and trigger filters

**Files:**
- Modify: `tagteam/router.py`, `tagteam/core.py` (`review`, `turn_end`, `plan_gate`), `tagteam/install.py`
  (consent prompt, `--reviewers`, `--authors`, `--yes`), `tagteam/live.py` (`outcome` for new notices)
- Test: `tests/test_routing.py`, `tests/test_install.py`, end-to-end tests

**Interfaces:**
- Consumes: `config.load`, `harnesses.detected()`, `quota.cooling(agent)` (exists)
- Produces:
  - `router.route(author, groups, cfg, headroom, load) -> (plan: dict, reason: str | None)`, where reason is
    one of `None`, `"no reviewers configured"`, `"no listed reviewer is available"`
  - `install.ask_consent(detected: list[str], preselected: list[str]) -> list[str]`, reading numbered choices
    from stdin
- Ordered mode: `pool = [a for a in cfg["reviewers"]["use"] if a != author and a in detected and not cooling(a)]`;
  each kind takes the first `needed(kind)` agents from `pool`. Quota mode: today's `eligible`/`route`, with
  candidates limited to `reviewers.use`.
- Core: if the author isn't in `authors.review`, or the change's kinds don't intersect `triggers.kinds`, return
  `None` (silent).

- [ ] **Step 1: Write the failing tests:**
  - `test_no_list_no_spawn`: a missing user config, `use = []`, and a repo-only config each produce zero
    `run_all` calls, and Stop returns the `no reviewers configured` notice.
  - `test_unlisted_never_spawned_either_mode`: `use = ["codex"]` with claude, copilot, and agy detected never
    plans anything except codex, in `ordered` or `quota` (including high-risk overflow).
  - `test_ordered_picks_in_list_order`: `use = ["copilot","codex","agy"]`, author `claude`, a large change
    (`per_change.large = 2`) gives `copilot, codex`. With copilot cooling it gives `codex, agy`.
  - `test_only_author_or_missing_listed` (Review Focus 3): `use = ["claude"]` with author `claude`, and
    `use = ["kiro"]` with kiro not detected, both return the `no listed reviewer is available` notice.
  - `test_legacy_config_without_consent_spawns_nothing` (Review Focus 5).
  - `test_author_and_trigger_filters`: author `codex` with `authors.review = ["claude"]` gives no review and
    `{}`. A plan-only change with `triggers.kinds = ["code"]` gives no review.
  - `test_install_consent`: interactive input `2,1` over detected `["codex","copilot"]` saves
    `use = ["copilot","codex"]`. `--yes` alone saves no `use`. `--reviewers codex` saves `["codex"]` with no
    prompt. Legacy `enabled` preselects, and the prompt shows `[x]` on those.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Write the consent prompt copy as: `These agents can review your changes. Each one
  you pick receives your diffs. Pick reviewers in order (e.g. 2,1), or press Enter for none:`
- [ ] **Step 4: Run the whole suite.** The end-to-end tests set `reviewers.use = ["copilot","agy"]` in their
  temp user config. All pass.
- [ ] **Step 5: Commit.** `git commit -m "Explicit reviewer consent; ordered routing by default"`

### Task 6: Loop stops: hourly reservation, digest memory, session lock, failure breaker

**Files:**
- Create: `tagteam/limits.py`, `tests/test_limits.py`
- Modify: `tagteam/core.py` (call the guards in `review()` before `runner.run_all`), `tagteam/runner.py`
  (report consecutive failures), `tagteam/live.py` (`review limit reached` outcome)

**Interfaces:**
- Produces in `limits.py`:
  - `reserve(run_id: str, session_key: str, now: float, cfg) -> bool`: under `state.locked("limits")`, prunes
    entries older than 3600s from `state/reservations.jsonl` (rewritten atomically), returns False if the count
    is `>= limits.max_reviews_per_hour`, else appends and returns True. Raises nothing: on any `OSError` it
    returns False and logs.
  - `digest(artifacts: list[dict]) -> str`: sha256 over the sorted `path + "\0" + diff`
  - `remembered(session_key, digest) -> dict | None`: the prior `{action, blocking: [...]}` for that digest
  - `remember(session_key, digest, action, blocking)`: stored in the session file under `"digests"` (max 50,
    oldest dropped)
  - `note_result(reviewer, ok: bool)`: two consecutive failures mean `quota.cooldown(reviewer, cfg cooldown_min)`
- `limits.session_lock(author: str, session_id: str) -> ContextManager[bool]`:
  - does a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on `state/locks/session-<author>-<safe sid>.lock`
  - yields True if acquired, False if another process holds it
  - the kernel releases it if the holder dies

  `core.review` takes it *first* and holds it until the run is recorded. If it gets False, it returns
  `("allow", None, run_dir, [])`: a skip, not a queue.
- Order in `core.review`:
  0. session lock (held through step 5)
  1. digest check (an approved digest is a silent skip; a blocked one re-issues the stored block and counts a
     round in `turn_end`)
  2. `route`
  3. `reserve`
  4. run
  5. record the run, then release the lock

- [ ] **Step 1: Write the failing tests:**
  - `test_parallel_reservations_never_exceed_ceiling` (Review Focus 2): with `max_reviews_per_hour = 5` and 4
    existing reservations, 8 subprocesses call `reserve`. Exactly 1 returns True, and the file has 5 lines.
  - `test_reservations_expire_after_an_hour`: entries at `now - 3601` don't count.
  - `test_reserve_state_error_means_no_review`: with `state.locked` patched to raise `OSError`, `reserve` is
    False, and end to end Stop returns `⚠ Not reviewed: review limit reached (…)` with zero spawns.
  - `test_crashed_run_still_counts`: a reservation with no finished run still counts toward the ceiling.
  - `test_approved_digest_skips_review`: same diff after an approved run gives zero spawns and `{}`.
  - `test_blocked_digest_reissues_block_and_counts_round`: same diff after a block (round 1) returns the
    identical `reason` with zero spawns and no new reservation. A third identical Stop (round 3 >
    `max_rounds_per_turn`) is allowed.
  - `test_failed_run_records_no_digest`: after a run where every reviewer failed, the same diff is reviewed
    again.
  - `test_concurrent_trigger_same_session_skipped`: two subprocesses start a Stop for (`claude`,`s`) together,
    released by a shared `multiprocessing.Barrier`-style file gate, with a fake reviewer that sleeps 1s and
    appends its pid to a file. Exactly one pid is recorded, and the other process returns `{}`. A different
    session in parallel is not blocked.
  - `test_breaker_after_two_failures`: two `note_result("agy", False)` put agy in cooldown, and one success in
    between resets the count.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** per the interfaces. Show `review limit reached` in `live.outcome` via
  `run["reason"]`.
- [ ] **Step 4: Run the whole suite.** All pass.
- [ ] **Step 5: Commit.** `git commit -m "Hard loop stops: hourly reservation, digest memory, session lock, breaker"`

### Task 7: Read-only sandbox for reviewers

**Files:**
- Create: `tagteam/sandbox.py`, `tests/test_sandbox.py`
- Modify: `tagteam/core.py` (`review()` opens `sandbox.checkout` *before* building prompts, then passes the
  sandbox path as both the `repo` argument of `build_prompt` and the `cwd` of `run_all`, so the real checkout
  path never appears in a prompt), `tagteam/runner.py` (`build_prompt` prints out-of-repo artifact paths as
  `~`-relative), `tagteam/core.py`
  (replace the `WORKING TREE CHANGED` log with `limits.disable(reviewer, reason)`), `tagteam/limits.py`
  (`disable`, `disabled() -> dict`, `clear_disabled()`). The router skips disabled reviewers.

**Interfaces:**
- Produces:
  - `sandbox.checkout(repo: str | None, tree: str | None) -> ContextManager[str]`. With a repo and tree it
    creates a commit with `git commit-tree <tree> -m tagteam-review`. Its env pins
    `GIT_AUTHOR_NAME = GIT_COMMITTER_NAME = "tagteam"`,
    `GIT_AUTHOR_EMAIL = GIT_COMMITTER_EMAIL = "tagteam@localhost"`, and
    `GIT_AUTHOR_DATE = GIT_COMMITTER_DATE = "@0 +0000"`, so it works with no git identity and is deterministic.
    The commit is unreferenced, so normal gc collects it. It then runs
    `git worktree add --detach <tmp> <commit>`, yields `<tmp>`, and finally runs
    `git worktree remove --force <tmp>`. It doesn't run a repo-wide `prune`, so other tools' worktree metadata
    is left alone. Otherwise it yields the original cwd.
  - `sandbox.wrote(repo, before_tree, touched_digests_before) -> bool`
- Reviewers see the snapshot taken at review start, the same content as the diff. Prompt paths stay
  repo-relative.

- [ ] **Step 1: Write the failing tests:**
  - `test_sandbox_works_without_git_identity`: with `HOME` set to an empty temp dir and
    `GIT_CONFIG_NOSYSTEM=1` (no `user.name` anywhere), `checkout` succeeds.
  - `test_reviewer_writes_land_in_sandbox_only`: patch a harness's `argv` to `["sh","-c","echo x > pwned.txt"]`
    and run a review end to end. `pwned.txt` isn't in the repo, the worktree directory is gone afterwards, and
    `git worktree list` shows only the main tree.
  - `test_prompt_never_contains_real_checkout_path`: capture `jobs` in a patched `run_all`. No prompt contains
    the real repo path, and each contains the sandbox path.
  - `test_real_tree_write_disables_reviewer`: a fake reviewer writes to the *real* repo by absolute path, which
    the sandbox can't stop, only detect. That reviewer appears in `limits.disabled()`, the next route skips it, and `tagteam doctor --clear` re-enables
    it (the doctor flag lands in Task 9; this test asserts `limits.clear_disabled()` here).
  - `test_non_git_dir_falls_back_to_flags_and_postcondition` (Review Focus 4): with a temp dir and no git,
    `checkout` yields that dir. A reviewer writing a touched outside-repo file gets disabled.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** per the interfaces.
- [ ] **Step 4: Run the whole suite**, plus one live smoke test: `tagteam doctor --probe` (Task 9) is deferred,
  so for now run one real review via the end-to-end pty script in the scratchpad and check that
  `git worktree list` is clean afterwards.
- [ ] **Step 5: Commit.** `git commit -m "Reviewers run in a disposable worktree; real-tree writes disable them"`

### Task 8: Spend capture and weekly display

**Files:**
- Modify: `tagteam/harnesses/{claude,codex,copilot,agy}.py` (`usage` parsers), `tagteam/runner.py` (put
  `usage` in `meta`), `tagteam/core.py` (`run["usage"] = {reviewer: usage}`), `tagteam/live.py`
  (`week_spend(now) -> (reviews, cost_usd, unreported)`, a line in `render_watch`, `render_swiftbar`, and
  `cli.status`)
- Test: `tests/test_spend.py`, fixtures `tests/fixtures/usage-{claude,codex,copilot}.json`. Capture one real
  output per CLI with a tiny prompt and save it as the fixture.

**Interfaces:**
- `Reviewer.usage(stdout, workdir) -> dict`:
  - claude: `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens` from the JSON envelope
  - copilot: `premium_requests` from `usage.json`
  - codex and agy: whatever their captured fixture shows; `{}` if nothing
- Display line, exactly: `This week: 14 reviews · ~$1.84 reported · 3 didn't report cost`. With nothing
  reported: `This week: 14 reviews · cost not reported`.

- [ ] **Step 1: Write the failing tests:**
  - `test_usage_parsers`: each fixture parses to the expected dict.
  - `test_week_spend_sums_known_and_counts_unknown`.
  - `test_watch_and_swiftbar_show_week_line`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.** All pass.
- [ ] **Step 5: Commit.** `git commit -m "Record reviewer usage; show weekly reviews and reported cost"`

### Task 9: `tagteam doctor`

**Files:**
- Create: `tagteam/doctor.py`, `tests/test_doctor.py`
- Modify: `tagteam/cli.py` (`doctor [--probe] [--clear]`), and each harness gets
  `logged_in: Callable[[], bool | None]` on `Harness` (`None` = can't tell cheaply)

**Interfaces:**
- `doctor.run(probe: bool, clear: bool) -> int`: prints one line per harness and returns 0 if every
  *configured* role is healthy, else 1.
- Line format: `codex   author ✓ hooks current · reviewer ✓ listed #1 · ⚠ approve hooks in codex (/hooks)`
- Checks:
  - detected
  - `logged_in()`
  - hooks present and pointing at an existing entry path
  - Codex trust (hook entries present but no trusted-hash record; read from `~/.codex` the way `DESIGN.md`
    describes)
  - listed in `reviewers.use`
  - disabled by the sandbox (with the `--clear` hint)
  - config errors
- `--probe` runs one tiny real review per listed reviewer inside `sandbox.checkout` with a canned one-line
  diff, and reports ok, timeout, or wrote-files.

- [ ] **Step 1: Write the failing tests** with harness detection and `subprocess.run` mocked:
  - `test_missing_entry_path_flagged`
  - `test_unlisted_reviewer_reported_not_error`
  - `test_disabled_reviewer_and_clear`
  - `test_exit_code_reflects_configured_roles_only`
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite**, then `tagteam doctor` on this machine. Check that the output matches
  reality (Codex hooks untrusted, four agents detected).
- [ ] **Step 5: Commit.** `git commit -m "tagteam doctor: per-harness health, --probe, --clear"`

### Task 10: README, upgrade notes, clean-install test

**Files:**
- Modify: `README.md` (rewrite for a new user, following the spec's README bullet), `DESIGN.md` (link the
  spec), `tasks/todo.md` (Phase 8 entry)
- Create: `scripts/clean_install_test.sh`

**Interfaces:**
- `scripts/clean_install_test.sh`:
  - if `uv` is missing, runs `python3 -m pip install --quiet uv` (or the user-local equivalent) so the script is
    self-contained in a bare `python:3.12` container
  - creates a temp `HOME`, `UV_TOOL_DIR`, and `UV_TOOL_BIN_DIR`
  - `uv tool install git+file://$PWD`
  - runs `tagteam install --yes --reviewers codex` against fake agent config dirs under the temp `HOME`
  - asserts: hooks contain the temp entry path, `config.toml` has `use = ["codex"]`, `tagteam doctor` exits
    with the expected code, `tagteam uninstall` leaves the fake configs byte-identical to their pre-install
    copies
  - on Linux (or `docker run python:3.12`), the same script must skip the SwiftBar and status line pieces
    without errors

- [ ] **Step 1: Write the script and run it.** `bash scripts/clean_install_test.sh`. Expected:
  `clean install: ok`.
- [ ] **Step 2: Run it in a Linux container.**
  `docker run --rm -v "$PWD":/src -w /src python:3.12 bash scripts/clean_install_test.sh`. Expected: `clean install: ok`, with `menu bar: skipped` in the output.
- [ ] **Step 3: Rewrite the README.** Sections: install, first run (consent), what you'll see (colors),
  configuration (table of keys, and what a repo file may set), limits, `doctor`, upgrade from a checkout
  (`uv tool install -e .`, then `tagteam install` to confirm reviewers; reviews pause until then), uninstall.
- [ ] **Step 4: Migrate Alec's machine.** `uv tool install -e .`, then `tagteam install`. Confirm the
  reviewers, then run `tagteam doctor`, then one real Claude turn with a change. Check that the menu bar and
  status line show the review.
- [ ] **Step 5: Commit.** `git commit -m "README for new users; clean-install test (macOS + Linux)"`

### Task 11: Issue follow-through in the status line, notices, and menu bar

Spec §5a and guard 2. Runs after Task 6, because it builds on its round counting and digest re-issue.

**Files:**
- Modify: `tagteam/core.py` (`turn_start`, `turn_end`, `plan_gate`, `review`), `tagteam/live.py` (`KINDS`,
  `outcome`), `tests/test_live.py`, `tests/test_core.py` (or wherever Task 6 left the core tests)

**Interfaces:**
- Session file gains `turn_baseline` (the snapshot from `turn_start`, never advanced) and `plan_prior` (the
  plan gate's last blocking findings). `prior` keeps its meaning: the last round's blocking findings.
- Run records (`runs.jsonl`, and `status.json` sessions) gain:
  - `prior: int` (open issues going into this run)
  - `confirm: bool`
  - `reason: str | None` (`"reverted"` for the no-review resolve)
  - `max_rounds: int`
- `live.KINDS` gains two kinds, in the same pastel family:
  - `resolved`: label `Issues resolved`, icon `✓`, the same colors as `passed`, sf `checkmark.seal.fill`
  - `unresolved`: label `Unresolved`, icon `⚠`, ansi 216, hex `#C46A3A,#FFC9A8`, sf
    `exclamationmark.triangle.fill`
- `live.outcome(run)` precedence:
  1. `reason == "reverted"` → `resolved`, `changes reverted`
  2. failed / unreviewed as today, plus `· N earlier issues not re-checked` when `prior`
  3. `confirm` with blocking → `unresolved`, `N issues still open after {max_rounds} fix rounds`
  4. block → `N issues to fix (round r/m)` when `prior == 0`, else `N issues still open after fixes (round r/m)`
  5. pass with `prior` → `resolved`, `all N fixed and re-checked` (plus `, M minor notes` when advisory)
  6. otherwise as today
- `turn_end` rules (`m = max_rounds_per_turn`, `r = round + 1`):
  - Review when `r <= m`, or when `r == m + 1` and `prior` is non-empty (the confirm review, `confirm=True`).
    `verdict.decide` already allows anything past `m`.
  - When the delta since the last round is empty and `prior` is non-empty, review `diff(turn_baseline, now)`
    instead (the rebuttal path), with `ev.last_message` as the author reply. If that is empty too, record a
    run with no reviewers and `reason="reverted"`. Spawn nothing, and clear `prior`.
  - The round-1 `min_changed_lines` skip is unchanged.
- `plan_gate` passes `plan_prior` as `prior`, stores the new blocking list back, and follows the same confirm
  rule using `plan_rounds`. Plan-gate state lifecycle:
  - `turn_start` rewrites the session file, so each user prompt starts with `plan_rounds = 0` and
    `plan_prior = []` (write both explicitly).
  - When the plan gate allows (an approval, a confirm review, or the round limit), it resets both to `0` / `[]`,
    so a later plan in the same turn starts fresh. Findings carry only between attempts at the same plan, that is,
    between a block and the next ExitPlanMode.
- Task 6's blocked-digest re-issue on a confirm round records an `unresolved` run without spawning.

- [ ] **Step 1: Write the failing tests:**
  - `test_status_shows_resolved_after_fix`: round 1 blocks with 2 findings. `live.segment` contains
    `✗ Changes requested: 2 issues to fix (round 1/2)`. Then a fix plus an approving reviewer gives
    `✓ Issues resolved: all 2 fixed and re-checked`.
  - `test_confirm_review_after_last_block`: with `m = 2`, block, then block. The third Stop (with changes) spawns
    once and returns allow. With one finding left, the segment shows
    `⚠ Unresolved: 1 issue still open after 2 fix rounds`; with none left, it shows `✓ Issues resolved`. A
    fourth Stop spawns nothing.
  - `test_no_confirm_review_after_pass`: round 2 approves. A third Stop with changes spawns nothing.
  - `test_rebuttal_without_changes_is_rechecked`: block, then a Stop with no file changes and
    `last_message="Not a bug: X is validated upstream"`. One spawn. The prompt contains the reply, the prior
    finding, and the round-1 diff.
  - `test_reverted_changes_resolve_without_review`: block, then the author restores the baseline. Zero spawns,
    and the segment shows `✓ Issues resolved: changes reverted`.
  - `test_failed_followup_mentions_open_issues`: block with 2, then every reviewer fails. The segment ends its
    outcome with `· 2 earlier issues not re-checked`.
  - `test_plan_gate_tracks_prior`: plan gate blocks, the next ExitPlanMode's prompt contains the prior finding,
    and an approval shows `Issues resolved`.
  - `test_plan_gate_state_resets_after_allow`: plan A blocks, then is approved. Plan B in the same turn gets
    round 1/2 with no prior findings in its prompt, and a clean review shows `✓ Review passed`, not
    `Issues resolved`. A new user prompt also resets a blocked plan's state.
  - `test_swiftbar_uses_new_kinds`: the menu bar header carries the `resolved` / `unresolved` sf symbol and hex.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** per the interfaces. `completion()` needs no change, since it already uses
  `live.summary`.
- [ ] **Step 4: Run the whole suite.** All pass.
- [ ] **Step 5: Live check.** One real Claude turn with a planted bug: watch the status line go
  `Changes requested` → `Reviewing` → `Issues resolved` after the fix.
- [ ] **Step 6: Commit.** `git commit -m "Track requested changes until a review resolves them"`
