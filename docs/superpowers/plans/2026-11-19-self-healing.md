# Self-healing tagteam Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reviews keep flowing without the user. Failing reviewers are covered by a fallback or demoted. Broken
breakers recover through probes and canaries. The user sees only what tagteam can't fix.

**Architecture:** A new `tagteam/health.py` owns advisory health: demotion from call history, `health.json`,
`heal.jsonl`, and the needs-you notice. A new `tagteam/heal.py` owns the detached tick: probes, canaries, pruning.
The write-safety history goes in `limits.py`, beside `disabled()`, as fail-closed `trips.json`. The fallback lives
in `core._review` inside the existing sandbox block. The views (`live.py`, `doctor.py`, `cli.py`) read
`health.json`.

**Tech Stack:** Python 3.11+ stdlib only. Tests use `unittest` + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-11-19-self-healing-design.md`

## Global Constraints

- Full suite: `python3 -m unittest discover -s tests -t .`. Focused runs: `python3 -m unittest tests.test_<mod> -v`. Never
  run bare `python3 -m unittest` (it pollutes the real `~/.tagteam`; see `tasks/lessons.md`).
- Every test module imports `tests.helpers` first (it sets `TAGTEAM_HOME`). `helpers._clean_run` wipes state per test.
- Mutation checks: baseline OK first, mutate a scratch copy (never the working tree), pass test names literally.
- Safety files (`disabled.json`, `cooldowns.json`, `trips.json`) read fail-closed via `state.read_dict` (`None` = unreadable).
  `health.json` is advisory: unreadable = everyone `ok`.
- Nothing in a hook may raise or add a probe/canary to a turn's critical path.
- Spec thresholds: demote when ≥3 of last 4 calls failed **and** the latest failed; fallback needs ≥90s left; tick every
  15 min; probe backoff 15→30→…→360 min; ≤2 probes per tick; ≤4 Copilot probes per day; prune run dirs >30 days;
  write trips "within 7 days" latch. Lock files are never deleted.
- Never route to a reviewer outside `reviewers.use`; never the author.

## Review Focus

1. **A fresh install with no `history.jsonl`, `probes.jsonl`, `health.json` or `trips.json`.** No one is demoted, no
   notice appears, and the tick runs without errors. Owned by Task 1 (`test_no_files_means_healthy`) and Task 4
   (`test_tick_on_empty_state`).
2. **Corrupt `trips.json`.** No canary runs, `disable` still disables, and the tick does not rewrite the file (that
   would erase the history). Owned by Task 2 (`test_unreadable_trips_blocks_canary_and_is_not_rewritten`).
3. **The fallback reviewer writes in its own checkout.** It is disabled exactly like a planned reviewer. Owned by
   Task 3 (`test_fallback_that_writes_is_disabled`).
4. **Two hooks fire at once when a tick is due.** Exactly one tick launches. Owned by Task 4
   (`test_maybe_start_launches_once_under_contention`).
5. **A needs-you issue persists across many turns.** The end-of-turn notice shows it once per 24h, while the status
   line keeps showing it. Owned by Task 5 (`test_notice_once_per_day`).

---

### Task 1: Health state and demotion in routing

**Files:**
- Create: `tagteam/health.py`
- Modify: `tagteam/router.py` (`candidates`/`_ordered`, `eligible`)
- Test: `tests/test_health.py`, `tests/test_routing.py`

**Interfaces:**
- Produces:
  - `health.calls(reviewer: str, n: int = 4) -> list[bool]`: the last `n` call outcomes, oldest first. Merges
    `history.jsonl` rows (`reviewer`, `ok`) with `probes.jsonl` rows (`reviewer`, `ok`, `probe: true`), sorted by
    `ts`.
  - `health.is_demoted(reviewer: str) -> bool`: `len(c) >= 4 and c.count(False) >= 3 and not c[-1]`.
  - `health.note_probe(reviewer: str, ok: bool, kind: str, detail: str = "") -> None`: appends to `probes.jsonl`.
    `kind` is `"probe"` or `"canary"`.
  - `health.load() -> dict`: `health.json`, or `{}` when it is missing or unreadable. Shape:
    `{"last_tick": float, "reviewers": {name: {"status", "since", "reason", "next_probe", "probe_backoff_min"}}, "noticed": {key: ts}}`.
  - `health.save(d: dict) -> None` (under `state.locked("health")`).
  - `health.event(reviewer: str, action: str, why: str) -> None`: appends
    `{ts, reviewer, action, why}` to `heal.jsonl`.
  - `health.events(n: int = 5) -> list[dict]`: the last `n` of those.

- [ ] **Step 1: Write the failing tests** in `tests/test_health.py`:
  - `test_no_files_means_healthy`: `is_demoted("agy")` is False, and `load()` returns `{}`.
  - `test_demoted_after_three_of_four_failures`: history rows `[T, F, F, F]` for agy make it True. `[F, F, F, T]` makes
    it False. `[F, T, F, F]` makes it True. Three rows makes it False.
  - `test_probe_success_clears_demotion`: history `[F, F, F, F]` then `note_probe("agy", True, "probe")` makes it False.
  - `test_unreadable_health_is_empty`: write `"{"` to `health.json`, and `load()` returns `{}`.
  - `test_events_roundtrip`: two `event()` calls, then `events(5)` returns both, oldest first.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_health -v`. Expected: ImportError / FAIL.
- [ ] **Step 3: Implement `tagteam/health.py`** with the interfaces above. Use `state.read_jsonl` and
      `state.append_jsonl`. Read only the last 7 days of history for speed (`since=now-7*86400`).
- [ ] **Step 4: Add the failing routing tests** in `tests/test_routing.py`:
  - `test_ordered_puts_demoted_last`: use `["codex","agy","copilot"]` with author claude, mock
    `health.is_demoted` True for codex, and `per_change.small=1`. The plan is `{"agy": …}`.
  - `test_ordered_demoted_still_used_when_alone`: only codex is listed, and it is demoted. The plan is `{"codex": …}`.
  - `test_quota_drops_demoted_unless_short`: in quota mode, mock `health.is_demoted` for one of two eligible
    reviewers, and that reviewer isn't picked for n=1. When n=2 it is.
- [ ] **Step 5: Implement.**
  - In `router._ordered`, stable-sort the pool by `health.is_demoted`.
  - In `router._quota`, after `eligible()`, drop demoted names when `len(pool) - len(demoted) >= max needed`.
    Otherwise keep them, but with their score multiplied by `r["tier_penalty"]`.
  - Leave `candidates()` unchanged, since the safety filter lives there.
- [ ] **Step 6:** Run `python3 -m unittest tests.test_health tests.test_routing -v` and then the full suite. Expected: OK.
- [ ] **Step 7: Commit** `git commit -m "Demote reviewers whose recent calls mostly failed"`.

### Task 2: Write-trip history (`trips.json`) and re-enable

**Files:**
- Modify: `tagteam/limits.py` (`disable`, `clear_disabled`, new functions)
- Modify: `tagteam/doctor.py` (`run(clear=True)` also clears trips)
- Test: `tests/test_limits.py`

**Interfaces:**
- Produces:
  - `limits.trips() -> dict | None`: `{reviewer: {"trips": [{"ts", "run_id"}], "latched": bool}}`, read fail-closed.
  - `limits.disable(reviewer, reason, run_id=None, latch=False)`: unchanged behavior, plus it records a trip. It
    latches when `latch` is True (a canary write) or when another trip is within 7×86400s. Lock order everywhere:
    `state.locked("disabled")`, then `state.locked("trips")` nested inside it. If `trips()` is None, it logs the problem
    and leaves the file alone, but the disable still happens.
  - `limits.canary_eligible() -> list[str]`: reviewers in `disabled()` that are not latched. Returns `[]` if either
    file is unreadable. An entry missing from `trips.json` counts as one trip at the `disabled.json` entry's `ts`,
    unlatched (the migration case).
  - `limits.trip_count(reviewer) -> int | None`: the number of recorded trips (1 for a migrated entry), or None if
    unreadable.
  - `limits.reenable(reviewer, expect_trips: int) -> bool`: holds both locks (same order). It removes the
    `disabled.json` entry only when `trips.json` is readable, the reviewer is not latched, and its trip count still
    equals `expect_trips` (no newer trip landed). Otherwise it returns False and changes nothing. `trips.json` is never
    modified.
  - `limits.clear_disabled()` also writes `trips.json` as `{}`.
- Consumes: the `core.py:299` call site passes `run_id=run_id`.

- [ ] **Step 1: Write the failing tests** in `tests/test_limits.py`:
  - `test_first_trip_is_canary_eligible`: `disable("copilot", "x", "r1")` → `canary_eligible() == ["copilot"]`, `trips()["copilot"]["latched"] is False`.
  - `test_second_trip_within_7d_latches`: the second trip lands 3 days after the first (patch `time.time`), so
    `latched` is True and `canary_eligible()` is `[]`.
  - `test_trips_8d_apart_do_not_latch`.
  - `test_old_disabled_entry_without_trips_is_eligible`: write `disabled.json` by hand with
    `{"copilot": {"reason": "x", "ts": now - 15*86400}}` and no `trips.json`. The result is `["copilot"]`.
  - `test_canary_write_latches_old_and_migrated`: two cases, both with `disable(..., latch=True)`. First, a real
    trip 15 days old. Second, a migrated `disabled.json` entry only. Each ends latched with `canary_eligible() == []`.
  - `test_reenable_refuses_after_newer_trip_or_latch`: take `n = trip_count`, then `disable` again (or latch it).
    `reenable(r, n)` returns False and the reviewer stays in `disabled()`.
  - `test_reenable_keeps_history`: disable, then `reenable(r, trip_count(r))`. Copilot is out of `disabled()`, and `trips()` still has
    one trip. A second `disable` within 7 days then latches.
  - `test_unreadable_trips_blocks_canary_and_is_not_rewritten`: write `"{"` to `trips.json`, then call `disable`. The
    reviewer is in `disabled()`, the file still reads `"{"`, and `canary_eligible() == []`.
  - `test_clear_resets_trips`: `clear_disabled()` → `trips() == {}`.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_limits -v`. Expected: FAIL.
- [ ] **Step 3:** Implement as specified. Pass `run_id` at `core.py:299`.
- [ ] **Step 4:** Run `tests.test_limits` and then the full suite. Expected: OK.
- [ ] **Step 5: Commit** `git commit -m "Keep write-trip history in fail-closed trips.json"`.

### Task 3: In-run fallback

**Files:**
- Modify: `tagteam/router.py` (new `fallback`), `tagteam/core.py` (`_review`, inside the `sandbox.checkouts` block, after `run_all`), `tagteam/live.py` (`status`/`watch` reviewer label)
- Modify: `tagteam/cli.py` (the `status` recent-runs line)
- Test: `tests/test_routing.py`, `tests/test_tagteam.py` (uses `HookRig`)

**Interfaces:**
- Consumes: `health.is_demoted` (Task 1).
- Produces:
  - `router.fallback(author: str, cfg: dict, taken: set[str]) -> str | None`: the first of `candidates(author, cfg)`
    not in `taken` and not `quota.cooling`, with `ok` before demoted.
  - The run record gains `"fallback_for": {new: failed}`, and the fallback joins `run["reviewers"]`.
  - `live.reviewer_label(run, name) -> str`: `"codex (for agy)"` when `name` is in `run.get("fallback_for", {})`,
    else `name`.

- [ ] **Step 1: Write the failing tests.**
  - `test_fallback_skips_taken_cooling_and_author`, in `test_routing`.
  - In `test_tagteam`, with HookRig and a fake `runner.run_all`:
    - `test_fast_failure_gets_fallback`: agy returns `{"ok": False, "timed_out": False, "duration": 5}` with result
      None. The second `run_all` call is `{"copilot": prompt}`. The run record has `fallback_for == {"copilot": "agy"}`,
      the outcome kind is not `"failed"`, and copilot's findings reach the verdict.
    - `test_timeout_gets_no_fallback`: agy times out, and only one `run_all` call happens.
    - `test_no_fallback_under_90s_left`: patch `core._clock` so 160s of the 240s deadline have elapsed when agy fails.
      No second call happens.
    - `test_failed_fallback_does_not_chain`: agy and its fallback copilot both fail fast. There are exactly 2 calls.
    - `test_fallback_that_writes_is_disabled`: the fake fallback writes a file into its `cwd`. Copilot ends up in
      `limits.disabled()`.
- [ ] **Step 2:** Run the focused tests. Expected: FAIL.
- [ ] **Step 3: Implement in `core._review`.** Do this inside the `with sandbox.checkouts(...)` block, before the
  `wrote = …` line.
  - Collect the failed reviewers that did not time out.
  - For each one, if `deadline - (_clock() - started_of_run) >= 90`, pick `router.fallback(ev.agent, cfg, set(plan) | used)`.
  - Open `sandbox.checkout(repo, before, cwd=repo or ev.cwd)` with an `ExitStack` entered for the rest of the block,
    and add the new path to `works`.
  - Build the fallback's prompt the same way the `jobs` dict builds one, using `plan[failed]`, then set
    `plan[new] = plan[failed]`.
  - Call `runner.run_all({new: prompt}, cfg, {new: path}, run_dir, remaining, on_done=update)` and merge the result
    into `outcomes`.
  - `live.tracking`'s `update` must accept an unknown reviewer; add it to `rec["reviewers"]`.
  - The write check, history rows, verdict and `failed` then pick up the fallback through `plan` and `outcomes` with
    no other change.
- [ ] **Step 4:** Use `live.reviewer_label` in `render_watch`, `render_swiftbar` and the `cli` status "Recent runs"
  line.
- [ ] **Step 5:** Run the focused tests and then the full suite. Expected: OK.
- [ ] **Step 6: Commit** `git commit -m "Hand a fast-failed review to the next healthy reviewer"`.

### Task 4: The heal tick

**Files:**
- Create: `tagteam/heal.py`
- Modify: `tagteam/doctor.py` (split `probe_one`), `tagteam/cli.py` (the `heal` subcommand; the hook calls
  `heal.maybe_start()`), `tests/helpers.py` (stub `heal._launch` the way `core._detach` is stubbed)
- Test: `tests/test_heal.py`

**Interfaces:**
- Consumes: Tasks 1–2 (`health.*`, `limits.canary_eligible`, `limits.reenable`, `limits.disable`).
- Produces:
  - `doctor.probe(name, cfg, seed=None) -> tuple[str, str]`: `(status, error_text)`. `status` keeps today's values:
    `ok | timeout | wrote-files | failed | unknown…`. `seed(repo_path, marker_dir)` plants files before the initial
    commit.
  - `doctor.probe_one(name, cfg)` keeps returning `probe(name, cfg)[0]`.
  - `heal.TICK_S = 900`, `heal.AUTH_ERR` (the spec's regex, compiled with `re.I`).
  - `heal.maybe_start(now=None) -> bool`: due when `now - health.load().get("last_tick", 0) >= TICK_S`. It takes a
    non-blocking `flock` on `locks/heal.lock` only to check-and-stamp `last_tick`. If it stamped, it calls `_launch()`.
    It never raises.
  - `heal._launch()`: a detached `Popen([sys.executable, "-m", "tagteam", "heal"], start_new_session=True, …)`.
  - `heal.tick(cfg, now=None) -> list[dict] | None`: takes a non-blocking `flock` on `locks/heal-run.lock` and holds
    it for the whole tick. Returns None at once if another tick holds it. Every entry point goes through it: the
    detached `heal` command and `doctor --fix`, which prints "a heal tick is already running" on None. Runs the
    spec's steps 1–5 in order and returns the events it logged. A canary reads `n = limits.trip_count(name)` before
    running and re-enables only through `limits.reenable(name, n)`.
  - `heal.canary_seed(repo, marker_dir)`: writes a `.claude/settings.json` Stop hook and a `.github/hooks/` hook, each
    `touch`ing `marker_dir/<name>`, plus a `.mcp.json` server whose command touches `marker_dir/mcp`.
  - `cli`: the `heal` subcommand (help `"internal: the detached self-heal tick"`) runs `heal.tick(config.load())`
    inside try/log, like `background`.

- [ ] **Step 1: Write the failing tests** in `tests/test_heal.py`. Mock `doctor.probe` and `harnesses.detected`.
  - `test_tick_on_empty_state`: no events and no exception. `health.json` has `last_tick`.
  - `test_demoted_probe_pass_restores`: agy is demoted and the probe returns `("ok","")`. Events include
    `restored agy`, and `is_demoted` is now False.
  - `test_probe_auth_error_needs_you`: the probe returns `("failed","Error: 401 Unauthorized")`. agy's status is
    `needs_you`, and the reason contains "log in".
  - `test_probe_failure_backs_off`: two failing ticks in a row with the clock advanced past `next_probe`. The backoff
    goes 15 → 30. When it is not yet due, no probe happens.
  - `test_max_two_probes_and_copilot_daily_cap`: three demoted reviewers give 2 probes per tick. Copilot gets no
    probe once 4 copilot rows from today are in `probes.jsonl`.
  - `test_canary_pass_reenables`: copilot is disabled and eligible. `probe` with the seed returns `("ok","")` and the
    marker dir stays empty, so copilot leaves `disabled()` and `trips()` keeps its trip.
  - `test_canary_marker_means_trip_and_latch`: the fake probe touches a marker. Copilot stays disabled and latched,
    with status `needs_you`.
  - `test_canary_inconclusive_stays_disabled`: the probe returns `("timeout","")`. Copilot stays disabled and not
    latched, and `next_probe` moves forward.
  - `test_prune_old_runs_not_locks`: a run dir with an mtime 31 days old is removed, and one 29 days old is kept.
    `locks/*.lock` stays untouched.
  - `test_missing_cli_and_not_logged_in_need_you`: a listed reviewer not in `detected()`, and one whose `logged_in()`
    is False, both get `needs_you`. Fixing them flips both back to `ok` on the next tick with a `restored` event.
  - `test_no_reviewer_for_author_needs_you`: every listed reviewer is disabled, so `needs_you` is keyed by author.
  - `test_concurrent_ticks_serialize`: one tick blocks inside a mocked canary, so a second `tick()` returns None.
  - `test_canary_pass_does_not_undo_a_newer_trip`: during the canary, the fake probe calls `limits.disable(name, …)`.
    The pass then fails to re-enable, and the reviewer stays disabled.
  - `test_prune_worktrees_under_registry_lock`: wrap `state.locked` and `sandbox._clear_stale` with recorders.
    `_clear_stale` is called only while `sandbox._registry_lock(repo)` is held, with
    `stale_s == max(7200, 2 * cfg["review"]["deadline_s"])`.
  - `test_maybe_start_launches_once_under_contention`: two threads call `maybe_start` together, and `_launch` is
    called exactly once.
  - `test_maybe_start_not_due`: `last_tick` is 60s ago, so the result is False and nothing launches.
- [ ] **Step 2:** Run `python3 -m unittest tests.test_heal -v`. Expected: FAIL.
- [ ] **Step 3: Implement `doctor.probe`** by refactoring the existing `probe_one` body. Capture stderr and return its
      tail as `error_text` (stdout's last error line when stderr is empty; reuse `runner._why`).
- [ ] **Step 4: Implement `tagteam/heal.py` per spec §3.**
  - Canary pass needs all three: `status == "ok"`, an empty `marker_dir`, and no `wrote-files`.
  - On a marker or `wrote-files`, call `limits.disable(name, "wrote in canary", run_id="canary", latch=True)`.
  - Stale hooks: reuse `doctor.author_part(h, doctor.hook_entries(h.hooks_path()))`. A `False` healthy flag gives
    `needs_you` with `→ tagteam install`.
  - Prune `state.path("runs")` children by mtime, then, for each repo that `runs.jsonl` saw in the last 7 days,
    `with state.locked(sandbox._registry_lock(repo)): sandbox._clear_stale(repo, max(7200, 2 * deadline_s))`. That is
    the same lock and threshold `sandbox.checkouts` uses. Skip a repo when this fails.
- [ ] **Step 5: Wire it up.**
  - In `cli.hook`, after `print(...)` on every handled phase, call `heal.maybe_start()` inside its own try.
  - Add the `heal` subcommand.
  - In `tests/helpers.py`, set `heal._launch` to a recorder that does nothing.
- [ ] **Step 6:** Run `tests.test_heal` and then the full suite. Expected: OK.
- [ ] **Step 7: Live check.** Run `TAGTEAM_HOME=$(mktemp -d) python3 -m tagteam heal`. It exits 0 and writes
      `health.json`. Then run `python3 -m tagteam doctor --probe` against the real home. It is unchanged in output.
- [ ] **Step 8: Commit** `git commit -m "Detached heal tick: probes, canaries, pruning"`.

### Task 5: What the user sees

**Files:**
- Modify: `tagteam/health.py` (`needs_you`, `notice`), `tagteam/live.py` (`segment`, `render_watch`, `render_swiftbar`), `tagteam/cli.py` (the turn-end notice; `doctor --fix`), `tagteam/doctor.py` (the health column; fix the name width)
- Test: `tests/test_health.py`, `tests/test_tagteam.py`, `tests/test_doctor.py`

**Interfaces:**
- Consumes: `health.load()`, `health.events()`, `heal.tick()`.
- Produces:
  - `health.needs_you() -> list[tuple[str, str]]`: `(key, text)`, e.g.
    `("copilot", "copilot: not logged in → copilot login")`.
  - `health.notice(now=None) -> str`: `" · ⚠ " + "; ".join(texts)` for keys not noticed in the last 86400s, or `""`
    when there are none. It stamps `noticed[key]`.

- [ ] **Step 1: Write the failing tests.**
  - `test_notice_once_per_day`: a `needs_you` entry gives a non-empty first `notice()`, `""` on the second, and
    non-empty again at +86401s.
  - `test_statusline_shows_needs_you_and_is_silent_when_healthy`: `live.segment` contains
    `"⚠ copilot: not logged in → copilot login"`, and with no issues contains no `"⚠"`.
  - `test_swiftbar_title_flags_and_health_block`: the title has `⚠` when something needs you. The dropdown has a
    `"Health"` line followed by one line per listed reviewer, and up to 5 `heal.jsonl` lines.
  - `test_watch_health_block`.
  - `test_turn_end_allow_carries_notice`: HookRig turn-end with a `needs_you` entry. The allow message ends with the
    notice once, and not on the next turn.
  - `test_doctor_columns_align`: with opencode installed, the output line starts with `"opencode "`.
  - `test_doctor_shows_health_and_fix_runs_tick`: `doctor --fix` calls `heal.tick` once and prints its events.
- [ ] **Step 2:** Run the focused tests. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `live.segment`: append `paint.kind("blocked", "⚠ " + text)` per `needs_you`.
  - `render_swiftbar`: put `⚠` in the title, and add a `Health` section before "Recent reviews".
  - `render_watch`: add a `Health` block after "Running".
  - `cli.hook`: when `ev.phase == "turn-end"`, the verdict action is `allow` and it has a `message`, append
    `health.notice()`.
  - Add the `doctor --fix` flag. Change the name format from `{name:8}` to `{name:9}`, and add a
    `· health <status> (next probe HH:MM)` part whenever the status isn't `ok`.
- [ ] **Step 4:** Run the focused tests and then the full suite. Expected: OK.
- [ ] **Step 5: Live check.** Run `tagteam doctor`, `tagteam swiftbar`, and `tagteam watch` (with `once`) against
      the real home, and paste the output into the PR.
- [ ] **Step 6: Commit** `git commit -m "Show only what needs you; health in watch, menu bar and doctor"`.

### Task 6: Docs and PR

**Files:**
- Modify: `README.md` (the health/self-heal section, plus `doctor --fix`), `DESIGN.md` (breakers section: demotion, fallback, trips, tick)

- [ ] **Step 1:** Write the docs. Reuse the spec's wording; the README stays user-facing.
- [ ] **Step 2:** Run the full suite. Expected: OK.
- [ ] **Step 3: Commit, push `self-healing`,** and open a PR against `review-log-fixes`. That base may need retargeting
      once PR #6 merges. Put the Task 5 live output in the PR body.
