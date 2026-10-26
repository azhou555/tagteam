# tagteam — implementation plan

Spec: `../DESIGN.md`. Each phase ends in something verifiable.

## Phase 0 — Spike: pin down each agent's real hook protocol
- [x] Logging hooks in all 4 agents; payloads captured to `tests/fixtures/` (Codex: schema from source, live pending trust)
- [x] Stop block works: Claude, Copilot, agy verified live; Codex per published schema
- [x] Reviewers read-only: Claude/Copilot/agy verified; Codex needed `approval_policy="never"` (read-only alone leaked via auto_review)
- [x] Structured output: claude/agy `structured_output`, codex `-o` file, copilot text → JSON extraction
- [x] `TAGTEAM_DEPTH` inherited by hook subprocesses
- [x] 280s Stop hooks survive in Claude, Copilot, agy
- [x] Quota sources (Claude statusline, Codex rollout, Copilot `gh api /copilot_internal/user`, agy self-meter)
- [ ] Capture each agent's quota-exceeded error text (only observable when a limit is hit; regex is a best guess)
- [ ] Plan-finalized events for Codex/Copilot/agy (none found; plans are reviewed at Stop like any file)

## Phase 1 — Core
- [x] Schema (`schema/findings.schema.json`), config defaults + per-user/per-repo overrides
- [x] Change detection: temp-index tree snapshots + out-of-repo touched files
- [x] Classifier (code/design/plan, risk)
- [x] Quota readers + reactive cooldowns
- [x] Router (tiers, reserve floors, headroom-weighted load, lenses)
- [x] Verdict (dedupe, thresholds, round cap)

## Phase 2 — Reviewer runner
- [x] Prompt rubrics per kind/lens; parallel spawn with `TAGTEAM_DEPTH=1`, timeouts, fail-open + cooldown
- [x] Real review of a buggy diff with each of the 4 CLIs (all found the off-by-one)

## Phase 3 — Adapters + installer
- [x] 4 adapters, tested against captured fixtures
- [x] `install`/`uninstall`: merge-only, tagged, backed up; verified byte-identical round trip on copies; installed for real

## Phase 4 — End-to-end (one per author)
- [x] Claude author → Copilot blocked → Claude rebutted (prompt demanded exact content) → allowed
- [x] Copilot author → agy blocked → Copilot fixed the bug
- [~] agy author → hooks fire and Copilot reviews, but Copilot approved the bug twice (model variance on a 1-reviewer small change)
- [ ] Codex author: needs the user to approve hooks via `/hooks` first
- [ ] Claude plan gate (ExitPlanMode) live test: not possible headless; verify in an interactive plan-mode session
- [x] Recursion: reviewer runs never start nested reviews (unit + depth-guard test; all live reviewer runs show no nested runs)
- [x] Loop cap (unit tests)

## Phase 5 — Tune
- [ ] A week of use; check `tagteam status` for load spread, false positives, and latency; adjust weights/thresholds

## Review (2026-10-25)
- 26 unit/integration tests pass; mutation checks (broken adapter ownership, primaries-first routing) make them fail as expected.
- Live smoke test: Claude 6.5s, Codex 9s, Copilot 12s, agy 48–92s (92s verified with `--effort medium` on the live command; 203s at default effort).
- Claude/Codex with unknown quota are never picked as reviewers (stale cache can't bypass the 50% floor).
- Known limits: small code changes get a single reviewer, so a weak review can miss a bug (seen with Copilot's
  default model). Copilot on the student plan costs 1 premium request per review with no cheaper model available.
  Quota-aware routing shifts load to agy as Copilot's quota drops.

## Phase 6 — Visibility + git context (2026-10-26)
Goal: see review status and counts without opening run dirs; stop false positives on git-only work.

Data (one source of truth for all three views):
- [x] `state/active/<run_id>.json` while a review runs: run_id, pid (display only), author, session, round,
      started, expires (= started + deadline_s + 60), reviewers with per-reviewer state (running / done ok /
      failed + secs). Liveness = the run holds an exclusive `flock` on `active/<run_id>.lock` for its whole life;
      the kernel drops it when the process dies (SIGKILL included), so pid reuse can't fake it. Readers try a
      non-blocking lock: acquired => owner dead => stale. `expires` is a backstop only. Removed in `finally`;
      stale records are skipped and best-effort unlinked. Test: a killed child leaves a record readers ignore.
- [x] `state/runs.jsonl`: one line per finished run (ts, run_id, author, session_id, reviewers, action,
      blocking/advisory counts, failed, duration).
- [x] `state/status.json`: small rollup updated at run end (today's run count, last result per session) so the
      2s status-line tick never scans growing logs. Hook errors (24h) counted at log time, shown as `⚠ N errors`.
- [x] Concurrency: every runs.jsonl append and status.json read-modify-write happens inside one
      `state.locked("runs")` section; status.json is replaced atomically (`write_json` temp + rename). Active files
      have one writer each (their run). Test: N concurrent run finishes leave N lines and a count of N.
- [x] `tagteam status` moves to run-level records (runs.jsonl) for counts and recent runs; per-reviewer load and
      latency stay on history.jsonl (reviewer calls), labelled as such.
- "Reviews" = runs (one per triggered review), not reviewer calls; `status`, `watch`, status line all agree.
- Active file is written in `review()` (covers plan gate); per-reviewer state updated from `run_all`'s
  `as_completed` loop (single writer); stale per the lock rule above.

Views:
- [x] Claude status line: wrapper appends one tagteam line under ponytail's output.
      Running: `tagteam ⟳ round 1/2 · copilot ✓ 11s · agy … 1m12s`
      Idle:    `tagteam ✓ clean (2 minor) · 3 reviews today` / `tagteam ✗ 2 blocking · …` / `tagteam · 3 reviews today`
      Session-scoped "last", global "today", `+1 running elsewhere` for other agents' reviews.
      Install adds `refreshInterval: 2` so it ticks while the Stop hook blocks (spike 2026-10-26: confirmed the
      status line re-runs during a blocking Stop hook). Re-install over an existing wrapper must still add it;
      uninstall restores the user's prior refreshInterval (or removes ours). Quota tee unchanged.
      Fast path: status-line entry imports only `state` (no runner/rubrics).
- [x] Completion message: allowed reviews return a one-line summary; Claude/Codex encode it as `systemMessage`
      (Stop and plan gate); block encoding unchanged. Codex: field present in binary, live display unverified
      (hooks not yet trusted). Copilot/agy stay silent on allow (no equivalent field).
- [x] `tagteam watch [--once]`: redraws every 1s — active runs with per-reviewer progress, today's counts by
      author and reviewer, last 5 runs.

Git context (fixes the false positive on the history rewrite):
- [x] turn-start records refs (`for-each-ref` + HEAD); review prompt gets a "Git activity this turn" section:
      new commits per ref, rewritten (non-fast-forward) refs, created/deleted refs, remote-tracking moves.
      A push to a named remote moves the local remote-tracking ref (verified 2026-10-26 with a bare remote), so
      pushes show up there. Limitation: `git push <url> …` without a configured remote updates no local ref and
      is invisible; we don't hook git itself.
      Cumulative from turn start (refs are not advanced per round), so round 2 still sees round 1's git work.
      Context only: ref changes never trigger a review.
      Rewrite = `merge-base --is-ancestor old new` fails. Section included in round-2 prompts too.

Verify:
- [x] Unit tests: active-file lifecycle incl. reviewer exception; runs.jsonl; segment for each state; encode
      systemMessage; watch --once render; git activity for commit, amend/rewrite, push, ref delete.
- [x] Live: real Claude turn — status line shows running → result, completion message appears, `tagteam watch`
      in another tab tracks it.

### Review (2026-10-26)
- 43 tests pass. Mutation checks: a dead owner reported as alive, the runs lock removed, and the git section
  dropped from the prompt each fail a test.
- Live (Claude 2.1.310, throwaway repo, real copilot review): status line showed
  `⟳ reviewing changes 1/2 · copilot … 2s · 3 reviews today` during the Stop hook. The transcript showed
  `Stop says: [tagteam] ✓ clean · copilot reviewed 13 changed lines in 7s`. Afterwards the status line, `watch`,
  and `status` all report 4 reviews today.
- Wrapper tick: ~40-55ms including ponytail. `install` backfilled 10 past runs into runs.jsonl.
- Found while testing: `~/.tagteam/statusline.orig` pinned ponytail 4.7.0, which ponytail's update had removed, so
  the ponytail segment was already broken. It now resolves the newest cached version (old copy saved as
  `statusline.orig.bak-20261026`).
- Not verified live: Codex completion message (Codex hooks still need `/hooks` approval). `tagteam watch` during a
  live run is covered only by the unit test.
