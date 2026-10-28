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
- [x] Codex author → Copilot blocked 2 planted bugs → Codex fixed both → agy allowed (2026-10-27). When the prompt
      demanded the code verbatim, Codex rebutted instead and the confirm round accepted it, as with Claude.
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

## Phase 7: Clearer status display + menu bar (2026-10-26)
- [x] One outcome model (running / passed / blocked / unreviewed / failed) with a plain-English detail. It
      drives the status line, `watch`, `status`, the completion message, and SwiftBar. "clean" is gone.
- [x] Status line: colored badges (amber/green/red/yellow), "N m ago", dimmed counts; `NO_COLOR` respected.
- [x] `watch`: same colors when stdout is a TTY.
- [x] SwiftBar plugin `tagteam.2s.sh`: SF Symbol + count (or elapsed time while running), with the error
      triangle in red. Dropdown: running, today's counts, recent runs (click opens the run dir), error log,
      open `watch`, refresh. `install` writes it into SwiftBar's plugin folder (sets `~/.tagteam/swiftbar` if
      none) and launches SwiftBar; `uninstall` removes it.
- [x] 47 tests pass (badge escapes, SwiftBar states, plugin install → execute → uninstall).
- [x] Restyle (user feedback): sage `tagteam`, pastel text color per status, no background blocks; menu bar
      gets a sage `person.2.fill` icon beside the status icon (light/dark color pairs).
- [x] Menu item didn't appear: SwiftBar ignores hidden folders and the plugin lived in `~/.tagteam/swiftbar`.
      Default is now `~/SwiftBar`; install moves the plugin out of the old folder and restarts SwiftBar.
- [x] Spotlight launcher `~/Applications/tagteam.app` (osacompile, ownership marker): always restarts SwiftBar,
      because a running SwiftBar can be stuck with no items and `open` on it only says "already open".
- [x] Visual check: user confirmed the menu bar item (2026-10-26). Before that, verified by item geometry from
      CGWindowList (no screenshot permission): a 79px Control Center item appears per display when SwiftBar loads.

## Phase 8: Packaging for teams (2026-10-26)
Spec: `docs/superpowers/specs/2026-10-26-packaging-design.md`.
- [x] Installable package and `tagteam` console script; hooks, status line and menu bar call the installed executable
- [x] Harness registry (claude, codex, copilot, agy); `--check`-gated migration of old checkout-style hooks
- [x] New config shape with a legacy shim; a repo file can only tighten; `tagteam config`
- [x] Reviewer consent: `reviewers.use` via `tagteam install` (`--reviewers`, `--yes`, prompt); no list, no reviews
- [x] Loop limits: 30 reviews/hour, digest memory, session lock, failure breaker
- [x] Per-reviewer worktree sandboxes and write detection; weekly spend lines
- [x] `tagteam doctor` (`--probe`, `--clear`, Codex trust); issue follow-through statuses (resolved / unresolved)
- [x] README rewritten for new users; DESIGN.md links the spec
- [x] `scripts/clean_install_test.sh` (temp HOME, `uv tool install`, install, doctor, uninstall byte-identical);
      `TAGTEAM_NO_MACOS_EXTRAS=1` makes install/uninstall skip SwiftBar, launcher and status line
- [ ] Linux container run of the clean-install script (Docker daemon was not running)
- [x] Migrate the author's machine, Claude side (2026-10-27): `uv tool install -e .`, `tagteam install
      --reviewers copilot,agy,claude,codex` with `routing.mode = "quota"`; `tagteam doctor` shows all hooks current.
      Live headless Claude turn after the install: agy reviewed in 56s, allowed; the menu bar's "no reviewers
      configured" warning was replaced by `✓ Review passed`, and the status line agrees. Between the packaging merge
      and this install, nothing was reviewed (no `reviewers.use` yet).
- [x] Codex side (2026-10-27): hooks approved in `/hooks`; `doctor` shows them trusted. Live `codex exec` turn:
      copilot reviewed in 8.8s, allowed. Note: `codex exec` with piped stdin waits for EOF even with a prompt;
      run it with `</dev/null`.
- [x] Existing user config with no `routing.mode` gets `mode = "quota"` on `tagteam install`; fresh installs stay `ordered`
- [x] `install` wires only detected agents by default (`--agents` overrides); skipped ones are named
- Follow-ups: an authors prompt in `install` (today only `--authors`); `--menubar` flag; Kiro, Cursor, OpenCode, Pi harnesses.

## Phase 9: Status reflects the real review state (2026-10-27)
Problem: a session showed `✗ Changes requested` for hours. Its round-3 confirm review was killed mid-run (no
verdict, no record, `finally` never ran), and `turn_start` dropped the open issues on the next prompt, so no later
turn re-checked them.

Data shape: the session file's `prior` is the one record of open issues. A cycle is closed by an allow
(open issues `[]`) or a confirm round (the last word; it stores `[]` too). Anything else, including a killed
run, leaves `prior` set.
- [x] Carry: `turn_start` keeps the previous turn's `prior`; carried issues are re-checked on any change, ignoring
      `min_changed_lines`; a no-change turn reviews nothing. Undoing a carried turn doesn't resolve its issues.
- [x] Only a real verdict closes the cycle: an allow, or a confirm round a reviewer actually answered. `review()`
      returns `None` for open issues when no reviewer gave a verdict (all failed, none available, state error).
- [x] Interrupted: a reader that finds a dead owner claims the record (one winner) and records
      `⚠ Review interrupted`; `record_run` sweeps first so it never lands after a later run.
- [x] A turn started mid-review inherits that review's open issues.
- [x] `[hooks] on_review` (user config only): detached `sh -c`, run JSON on stdin, `TAGTEAM_OUTCOME`,
      `TAGTEAM_SUMMARY`, `TAGTEAM_DEPTH=1`; never blocks or fails the hook; backfill never fires it.
- [x] 239 tests pass; each feature's core line mutation-checked.
- [x] Live (2026-10-27, headless Claude, throwaway repo): round 1 blocked 2 issues; round-2 hook SIGKILLed; status
      line showed `⚠ Review interrupted · 2 earlier issues not re-checked`; next turn's 1-line edit was reviewed
      with the carried issues and showed `✓ Issues resolved: all 2 fixed and re-checked`. on_review received all
      four runs with correct outcome/summary.
- [x] Change snapshots skip untracked nested git repos (they became gitlinks, seen with `.claude/worktrees/`);
  tracked submodules still diff. Unit test plus a live snapshot of this checkout with a nested repo dropped in.

## Phase 10: Blocking and non-blocking reviews (plan, 2026-10-27)
Goal: faster development. An issue in a large change, a plan or a design blocks. A small error in code logic doesn't
make the agent wait or send it back. Users and agents can always tell which kind they're looking at.

Data shape: one `tier(kind, artifacts, rv)` → `small | large | high_risk | docs` replaces the branching in
`router.needed`; per-change reviewer counts and `review.blocking` both look it up. Each run records
`mode = "block" | "advise"`. Each open issue in `prior` carries a `gate = "block" | "advise"`.

Gate of each finding a review returns (computed in `verdict.decide`, before the blocking set is formed):
1. `severity == "blocker"` → `block`, in any tier. A blocker is not a small error; it blocks the next Stop if the
   review that found it was a background one.
2. Else, if it matches a carried `advise` issue (the same file, line and claim test `dedupe` uses) → `advise`.
3. Else → the review's own mode.
The rules apply in order; the first that matches wins. Only `block`-gated findings at or above `block_at` enter
the blocking set; `advise` ones are shown as suggestions. So a re-checked suggestion stays a suggestion unless a
reviewer now rates it a blocker, in which case it is upgraded to `block`. A new distinct issue in a blocking review
still blocks.

- [x] Config: `review.blocking = { small = false, large = true, high_risk = true, docs = true }`. A repo
      `.tagteam.toml` may only switch a tier to true (adds scrutiny). The plan gate is always the blocking kind;
      the existing round limit and confirm round still apply to it, as they do to every blocking review.
- [x] Non-blocking turn end: snapshot, start the review as a detached process (own session, like `on_review`),
      return allow immediately with a notice ("Reviewing in the background; results appear in the status line").
      Its deadline no longer has to fit the Stop hook timeout.
- [x] The background review writes its findings to `prior` through `_write_back` (finishing after a new turn has
      started becomes the normal path; test it fully). It uses none of the turn's blocking rounds. If it left
      any `block`-gated issue (a blocker), the next Stop blocks with it as round 1, even when that turn changed
      nothing, and the usual round limit and confirm round follow.
- [x] Session lock: the background review holds the session lock for its whole run, as reviews do today, and
      writes back under it, so two reviews of one session never overlap. It has its own deadline, so it can't
      hold the lock forever; a killed one releases the flock and is recorded as `Review interrupted`.
      The next Stop waits for the lock (up to `deadline_s`) instead of skipping, then reviews its own turn.
      If the lock is still held at the deadline, it starts no review: it records `⚠ Not reviewed: earlier review
      still running` and keeps its turn's baseline as `pending_baseline`; the next `turn_start` uses that as the
      baseline, so the following review covers both turns' changes.
- [x] Delivery to the agent: turn start adds open non-blocking issues to the prompt as context, labelled "not
      blocking; fix when convenient". Verify per harness which turn-start hooks accept added context (Claude does;
      Codex, Copilot and agy are unchecked). Where a harness can't, the issues still show in the status surfaces.
- [x] Awareness for users, distinct on every surface (status line, menu bar, `tagteam watch`, `tagteam status`):
      - running: `⟳ Reviewing in background` vs `⟳ Reviewing` (agent waiting)
      - findings: `✗ Changes requested` (blocking, red) vs a new `● Suggestions: N issues, not blocking` (blue)
      - the menu bar lists open issues grouped as Blocking / Not blocking; each run row shows its mode
      - the block message and the injected context both say which kind they are
      - `on_review` gets `TAGTEAM_MODE`
- [x] README: the two kinds, the defaults, how to change them.
- [x] Tests: tier lookup; repo file can't lower a tier; a small change returns allow at once and its findings
      land in `prior` with `gate=advise`; a carried advise issue re-checked at major stays `advise` and doesn't
      block, while the same issue re-checked as a blocker is upgraded to `block` and blocks; the next Stop waits for a running
      background review; at the lock deadline it records `Not reviewed` and the next review covers both turns;
      a blocker from a background review blocks the next Stop; a killed background review records
      `Review interrupted`; surface strings per kind.
- [x] Live (2026-10-27, headless Claude, throwaway repo, Claude hooks reinstalled at 540s): an 11-line buggy edit
      ended the turn in 7s; status line `⟳ Reviewing in background · copilot`, then `● Suggestions: 1 issue, not
      blocking` (major, gate advise); the next prompt carried it as context. A 185-line change blocked rounds 1-2,
      round 3 confirm allowed. A 4-line edit was skipped by `min_changed_lines`, as before.
- [x] Found live: a review of one file was handed every open suggestion and dropped an unfixed one on another
      file. Suggestions are now re-checked only by a review of their own file, and a confirm round closes only
      the blocking cycle (verified live: the bank.py suggestion survived the refunds.py blocking cycle).
- [x] Codex hooks have the 540s Stop timeout (installed 2026-10-27 23:57; Stop re-approved at 23:57:30). Doctor
      kept saying "re-approve" because the install snapshot recorded every hook, including the two the
      install left alone; it now records only the hooks an install changes (2026-10-28).
- Context injection per harness (checked 2026-10-27): Claude, UserPromptSubmit `additionalContext` (yes). Codex,
  yes: codex-rs `hooks/src/engine/output_parser.rs` `parse_user_prompt_submit` reads
  `hookSpecificOutput.additionalContext`, and the `user-prompt-submit.command.output` schema has `hookEventName`
  const `UserPromptSubmit`. Copilot, no: the GitHub hooks reference says config-file `userPromptSubmitted` hooks
  "have their output dropped". agy, no: nothing documents PreInvocation output. `Author.can_add_context` records it.
- Choices the plan left open: a review stays blocking while any open issue is block-gated, so a blocker's small
  fix is re-checked in the foreground, not in the background. A background review runs only with a repo, a
  snapshot and a `reviewers.use` list, else in the foreground (still advise-gated); if its reviewers would run
  in place (no sandbox) it records `⚠ Not reviewed: no sandbox for a background review`. The plan gate takes its
  own session lock lane (`<session>:plan`), so a running background code review never delays or skips a plan;
  the two write different session fields, each under the state lock. Reviewer counts now come from
  `per_change[tier]`, where `high_risk` wins over `docs`/`large`: a user with `large > high_risk` gets
  `high_risk` reviewers for a risky large change (it was the max of the two). Stop hook timeout is now
  `2 * deadline_s + 60`; run `tagteam install` before the live test.
- 265 tests pass. Mutation-checked: repo rule for `blocking`, tier precedence, the three gate rules and the
  block-gated action, lock wait, pending baseline, `pass_fds` handoff, owed delivery, blocker carried as owed,
  mismatch carry, blocking-cycle mode, pinned tree, background `changed` suppression, no-sandbox skip, the
  `reviewers.use` guard, context injection (core and Codex encode), and each surface string.

Decided: a blocker-severity finding blocks in any tier (gate rule 1). The severity label has to mean something,
and a line count can't vouch for a change being safe.

## Phase 11: A guided `tagteam install` (2026-10-28)

Today install writes hooks (and the routing migration) before it asks who may review, so Ctrl-C at the picker
leaves hooks with no reviewers; it never says what tagteam is; `--dry-run` dumps whole files (all of
~/.claude/settings.json, other tools' hooks included); the picker shows bare names.

Shape: **look → choose → confirm → apply → next steps.** Nothing is written before the confirm.
Decided with three subagent reviews (new user, maintainer, code fact-check): first cut only.

- [x] Intro: what tagteam is, the version, "your diffs go only to the reviewers you pick", one line per agent
      (installed · credentials found / sign-in unknown · tagteam hooks yes/no). Sign-in can only be "found" or
      "unknown": `signed_in_if` never returns False, and Claude on macOS keeps credentials in the keychain.
- [x] Stop before any prompt if fewer than two agents are installed, with the fix.
- [x] Reviewer picker: one note per agent (model/effort from config, credentials found or unknown).
- [x] `plan(agents)`: run the parse checks and render every hook file without writing (harness `install`
      returns `(path, old, new)` from `base.save` in a dry run). One summary: per file "add/update tagteam's
      hooks", "route your status line through tagteam" when statusLine changes, config keys, the menu bar line.
- [x] `Apply? [Y/n]` (skipped with `--yes`); `--dry-run` prints the summary plus a unified diff per file and
      stops. Ctrl-C before apply says nothing changed.
- [x] Apply order: routing migration, hooks, config, then menu bar. Each file is already atomic (`base.save`
      writes a temp file, re-parses, replaces) and backed up; the "updated" line names the backup. A failure
      names the file and the backups and exits 1; re-running converges (only tagteam's entries are replaced).
      A menu bar failure is reported and leaves hooks and config in place. No multi-file rollback (decided:
      it would also have to undo the status line originals and Codex trust snapshot; a re-run fixes it).
- [x] Next steps: Codex /hooks approval when Codex was wired, a concrete first test, `tagteam doctor` output,
      where the config lives.
- [x] `tagteam --help` and bare `tagteam` describe the package and the common commands.
- [x] Tests: summary and diff in a dry run write nothing; declining the confirm writes nothing; the picker
      shows notes; precheck stops with one agent; a menu bar failure keeps the hooks; help text.
- [x] Live: dry run and a real re-run on this machine; clean-install script in a temp HOME.

Deferred: authors picker (default stays every wired agent), review-style presets (`config.save_user` writes
only lists; Light can't mean "nothing blocks"), per-reviewer model prompt, `--menubar` flag.

Results (2026-10-28): 283 tests pass. Mutation-checked: the confirm gate, dry run writing nothing, a menu bar
failure keeping the hooks, and unchanged files being skipped. Clean-install script ok from a temp HOME.
Live on this machine: the dry run showed every hook file up to date and `reviewers.use stays agy, copilot`;
the real run wrote only the menu bar plugin and launcher; doctor healthy (Codex still asks to re-approve
hooks, as before this phase). Found live: install rewrote and backed up files it reported as up to date;
apply now skips them. Incident during this phase: see lessons.md (a mis-invoked test run wrote to the real
~/.tagteam; repaired, quarantined in ~/.tagteam/state-test-pollution-20261028/).
