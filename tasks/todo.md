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
- [x] Claude plan gate (ExitPlanMode) live test (2026-10-28, interactive session): agy + copilot requested 3
      changes to the plan (round 1/2); the revised plan was re-checked and all 3 resolved (round 2).
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
- Follow-ups: ~~an authors prompt in `install`; `--menubar` flag~~ (Phase 13); Kiro, Cursor, OpenCode, Pi harnesses.

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

## Phase 12: Agent colors in `watch` and the menu bar (2026-10-28)

- [x] One palette: `live.AGENTS` gives each agent an ANSI code and a SwiftBar light/dark hex pair.
- [x] `watch`: author and reviewer names in their agent's color (running rows, progress, counts, recent runs);
      `Paint.agents` pads by the uncolored width so columns don't shift.
- [x] Menu bar: SwiftBar colors a line's text as one, so each name gets a `:circle.fill:` dot colored through
      `sfcolorN`, in name order. Lines keep their outcome color.
- [x] Tests: colored watch = plain watch with the escapes stripped; each name colored in the recent row; menu
      row dots map to the right agents (both checked against mutations). 287 OK.

## Phase 13: Install follow-ups: authors picker and `--menubar` (2026-10-28)

- [x] Authors picker after the reviewer picker, terminal only (`--authors` and `--yes` skip it; scripts use the flag).
      Lists the agents being wired, preselects `authors.review`; agents not wired now keep their setting. No order.
- [x] `--menubar` / `--no-menubar` (default on): off leaves SwiftBar and the launcher untouched; the summary says so.
- [x] Summary always shows whose turns are reviewed (`authors.review stays …` when unchanged).
- [x] Tests: picker on a pty without numbers; keep/unchanged/none cases; `--no-menubar` writes no plugin; CLI flag.
      Mutation-checked: keeping unwired agents, `--no-menubar` skipping SwiftBar, the picker wired into `run`,
      marks instead of numbers, unchanged = nothing saved, `--yes` skipping it, the summary's "stays". 293 OK.

## Phase 14: Background review results in Codex (2026-10-28)

Codex's status line takes only built-in items, so a background review's result never reached the Codex
terminal, and its turn-end notice pointed at a status line Codex doesn't have.
- [x] `Author.has_status_line` (Claude only). Other agents' turn-end notice: "the result shows at your next prompt
      (live: tagteam watch)".
- [x] A finished background review on an agent without a status line stores its one-line result in the session;
      the next UserPromptSubmit shows it once as a `systemMessage` (Codex renders it as `↳ Hook · …`; checked in
      codex-rs `hooks/src/events/user_prompt_submit.rs`). With open suggestions, the same output carries both the
      model context and the notice. Blockers get no notice: they block the next Stop. Claude is unchanged.
- [x] Tests: once-only, suggestions carry both fields, blocker gets none, Claude gets none, Codex text. All seven
      mutations caught. 296 OK.
- [x] Live check in the Codex TUI: a small change, then the next prompt shows the result (confirmed 2026-10-28, dj-rara run 20261028T165517-6818).

## Phase 15: see background results sooner (2026-10-28)
- [x] Menu bar refreshes every second: plugin renamed `tagteam.1s.sh` (a refresh costs ~40 ms); install
      removes tagteam's own `tagteam.2s.sh` (two intervals would show two icons), uninstall removes either.
- [x] `hooks.on_review` gets `TAGTEAM_AUTHOR`; README example notifies only for background reviews of
      agents without a status line. Enabled in the user's config; test notification fired for codex, skipped for claude.
- [ ] Live: next Codex background review pops a macOS notification.

## Phase 16: New agents, review-style presets, reviewer model prompt (plan, 2026-10-28)

**Paused 2026-10-28 at the user's request.** Resume only when the user says so; the Cursor, Kiro and Pi spikes also wait on their sign-ins. Nothing is running.
Order: Cursor, OpenCode, Kiro, Pi, then install work. Presets as written (decided 2026-10-28).
Every spike captures real payloads into `tests/fixtures/`; those fixtures are the research data.

### 16a: Cursor (`cursor-agent` 2026.10.22 installed)
- [ ] Spike: event names and hook file location from current docs, then a logging hook in `~/.cursor/hooks.json`.
      Record which events map to turn-start / touch / turn-end, the payload fields (session id, cwd, prompt,
      tool-arg paths), whether turn-end can block and through which field, the timeout ceiling (need ~280s),
      whether `TAGTEAM_DEPTH` reaches the hook, and where credentials live (`logged_in`).
- [ ] Reviewer spike: find a truly read-only headless mode (`-p` alone allows write and shell; try `--mode ask`
      or `--mode plan`, plus `--sandbox enabled`). Prove read-only live, as was needed for Codex. Check `--trust`
      in a fresh worktree sandbox so runs don't hang on a prompt. Structured output via `--output-format json`; usage.
- [ ] Adapter `harnesses/cursor.py` tested against the captured fixtures; registry detects the `cursor-agent`
      binary (the CLI name differs from the agent name).
- [ ] Wire the name everywhere it is hard-coded: `cli.py` hook choices, `live.AGENTS` palette,
      `config.default.toml` (reviewer table, routing agent with a quota source, `authors.review`), doctor/install lines.
- [ ] Live end-to-end: Cursor author → reviewer blocks a planted bug; Cursor as reviewer finds an off-by-one.

### 16b: OpenCode (1.17.11 installed)
- [ ] Spike: hooks are JS plugins, not a JSON file. Get the plugin event API from current docs; a logging plugin
      that shells out to `tagteam hook opencode <phase>`. Same facts as Cursor, plus whether turn-end can block
      at all (if not: a non-blocking author with background results, like Codex).
  Spike notes (1.17.11, project plugin in a scratch repo): `chat.message` gives `sessionID` and the prompt in
  `parts[].text` (`opencode run` wraps it in literal quotes); plugin gets `directory`/`worktree`; `TAGTEAM_DEPTH`
  reaches the plugin and its children. Upgraded to 1.19.5. Blocking: on a long-lived server (`opencode serve` +
  `run --attach`, as in the TUI) a `client.session.prompt` follow-up from `session.idle` runs as a new user turn
  (it also fires `chat.message`, so turn-start must not reset the round count); headless `opencode run` exits at
  idle, so there it can only be background. Context: pushing a text part in `chat.message` reaches the model.
  Reviewer: `--pure` loads no plugins; `OPENCODE_CONFIG_CONTENT` permission deny for edit/bash removes those
  tools (proven: build agent could not write); also deny task/websearch. `--format json`: text parts + step_finish
  tokens/cost. Fixtures: tests/fixtures/opencode-*.json. Spike files only in the scratchpad.
- [x] Install shape: one tagged plugin file, `~/.config/opencode/plugins/tagteam.js`, rendered from
      `tagteam/data/opencode-plugin.js` (backup, dry-run diff, uninstall removes only a file carrying the tag).
      `base.write` saves any text; install's summary and doctor's hook check read a non-JSON hook file.
- [x] Reviewer: `env OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_CONFIG_CONTENT=… opencode run --pure --agent
      tagteam-review --format json`. Live: `OPENCODE_CONFIG_CONTENT` alone lost to a repo `opencode.json` that
      allowed edits (the reviewer wrote a file), and so did `OPENCODE_PERMISSION`; with project config off the
      write tools are gone, and the agent only our config defines kept them denied under a hostile
      `OPENCODE_CONFIG` file. Found a planted off-by-one in 4.2s; output and usage parse
      (`tests/fixtures/opencode-review.jsonl`).
- [x] Adapter, registry, wiring (cli choices now come from the registry, palette khaki, config tables).
- [x] Plugin, live on 1.19.5 (`opencode serve` + `run --attach`): a pushed context part needs `id`, `sessionID`
      and `messageID`, or OpenCode fails the user's whole turn; with them it reaches the model. Subagent sessions
      report as their root (touches attributed, no turn-start or turn-end of their own). Headless `run` calls no
      hook. A toast posted to `/tui/show-toast` shows in an attached TUI.
- [x] Live end-to-end with the real plugin and `tagteam` (project-level plugin in a scratch repo; the classifier
      refused writing the global plugin, so `~/.config/opencode/plugins` was never touched): high-risk `auth.py`
      with a planted bug, agy + copilot blocked round 1, OpenCode fixed it, round 2 copilot objected that the fix
      broke "exactly this content", OpenCode reverted with a rebuttal, round 3 confirm allowed (runs
      20261031T224148-10a7, -8859, -c4cf). Round count held across the follow-up prompts. Background: a 5-line
      `util.py` got agy's suggestion, and the next prompt carried it as a synthetic part.
- [x] Second pass, live with a fake tagteam:
      - A fix round now carries the user's agent and model (tested with `--agent plan -m …glm…`).
      - An aborted turn (`POST /session/:id/abort`) used to be restarted by the block prompt. OpenCode sends
        `session.error` MessageAbortedError and then *two* idles, so the plugin skips turn-end until the next
        prompt, as with Claude's Esc. The next turn is reviewed again.
      - A user prompt during a running review drops the block prompt.
- [x] Reviewer `$PWD` bug: OpenCode takes its directory from `$PWD`, which `runner._spawn` inherits from the
      hook, so 3/4 reviews ran in the wrong folder and every read of the review checkout was auto-rejected as
      external. `env -u PWD` fixed it (4/4 found the bug, reading files). This invalidated the first
      Python-driven hostile-repo test. Redone: a repo `.opencode/opencode.json` plus `.opencode/agent(s)/
      tagteam-review.md` allowing bash ran `touch` without isolation (control) and had no bash or write tool
      under tagteam's argv. `snapshot: false` stops ~22 snapshot files per review; runs are titled
      "tagteam review".
- [ ] Install for real (`tagteam install --agents opencode`) and try it in the plain TUI: the classifier refused
      the write to `~/.config/opencode/plugins`, so the user runs it.
- [ ] OpenCode 2 port (mostly done, see below). 2.2.0 is installed now and both halves fail (verified 2026-11-01):
      - Plugin: the log says `failed to load plugin … tagteam.js … Plugin must export a default definition with
        an id and an effect or setup function`, so no hook has ever run in the TUI.
      - Reviewer: `Unrecognized flag: --pure`. `--variant` is gone too (now `-m provider/model#variant`), and
        `run` now talks to a shared background service unless you pass `--standalone`.
      Done 2026-11-01. Every free OpenCode model was out of quota (~9.5 h), so the live checks drove OpenCode
      2.2.0 with a scripted local OpenAI-compatible model (scratchpad `mock.py`) through `opencode serve` and
      `run --server`.
      - [x] Plugin: `export default { id: "tagteam", setup, server }`; v2 loads it (log shows no load error).
            `server()` is the 1.x code, unchanged except that it's no longer a named export.
      - [x] v2 `setup(ctx)`, live: the prompt hook → turn-start (intent recorded without run's quotes);
            `tool.hook("execute.after")` → touch (it carries `sessionID`; v2 tools take `path`, which
            `PATH_KEYS` already has); `session.execution.succeeded|failed` → turn-end;
            a block → `ctx.session.prompt`, which starts a new turn that the prompt hook sees with the
            `[tagteam]` marker (agent and model are per session in v2, so the 1.x `picks` map is not needed);
            context → `session.synthetic({resume: false})` from inside the prompt hook, which lands just before
            the user's message. Interrupted turns aren't subscribed, so they end unreviewed. Round trip: high-risk
            `auth.py` with a planted bug, agy + copilot blocked round 1, the fix prompt reached the model, the
            mock changed nothing, round 2 blocked, round 3 allowed at the cap (run 20261101T094236-8216).
            Background: an 11-line `stats.py` got agy's suggestion (run 20261101T094452-f96a), and the next prompt
            carried it as a synthetic message. One server, two folders: a session in another folder got no tagteam
            activity.
            Not available in v2: a server plugin has no toast API, so `systemMessage` notices are dropped.
      - [x] Reviewer: `run --standalone`, no `--pure`, effort as `-m model#effort` (dropped without a model).
            The config is now v2-shaped (`permissions` rules, `agents`) and an allowlist: deny `*`, then allow
            read/glob/grep. Control: a hostile repo (`opencode.json` + `.opencode/opencode.json` + agent file all
            allowing `*`) plus a hostile `OPENCODE_CONFIG` let a `tagteam-review` agent write `pwned.txt`. Under
            tagteam's argv the model is offered only read/glob/grep/skill and nothing is written. Both
            `OPENCODE_DISABLE_PROJECT_CONFIG` and `OPENCODE_CONFIG_PROJECT_DISABLE` drop project config in v2.
            `snapshot: false` is still accepted. The `--format json` events (`text`, `step_finish` with tokens
            and cost) are unchanged, so the parsers and fixture stand; the fixture is not re-recorded because
            no real model had quota.
      - [ ] Planted-bug review with a real OpenCode model as reviewer (needs quota back).
      - [ ] The user's real TUI after `tagteam install --agents opencode` rewrites the global plugin.
      - Open: `opencode run` without `--standalone` now uses the shared service, where the plugin runs as in
        the TUI, so a headless run's turn is reviewed and a block continues the session after `run` exits. The
        1.x `headless()` check can't tell from inside a v2 server.

### 16c: Kiro and Pi (decided: install both and spike live; the user signs in)
- [ ] Install `kiro-cli` and `pi`; same spike, adapter, wiring and live end-to-end as Cursor.

### 16d: Review-style presets in `tagteam install`
- [ ] `config.save_user` writes scalars and inline tables, not only lists (today it can't express "nothing blocks").
- [ ] Presets write exactly two keys in the user's `[review]` table; `block_at`, `large_lines` and the rest stay as they are:

      | preset   | `review.per_change`                                  | `review.blocking`                                          |
      |----------|------------------------------------------------------|------------------------------------------------------------|
      | Light    | `{ small = 1, large = 1, high_risk = 1, docs = 1 }`  | `{ small = false, large = false, high_risk = false, docs = false }` |
      | Standard | key removed (follows the packaged default)           | key removed (follows the packaged default)                 |
      | Strict   | `{ small = 2, large = 2, high_risk = 2, docs = 2 }`  | `{ small = true, large = true, high_risk = true, docs = true }`     |

      Standard writes nothing and removes these two keys, so it follows future default changes (today:
      per_change 1/2/2/2, blocking false/true/true/true). No preset name is stored: the picker reads the two keys
      from the user file only (not merged defaults). Both absent = Standard; both equal to a row = that preset;
      anything else = "custom", which is preselected and kept unless the user picks a preset.
      `--preset light|standard|strict`; `--yes` without it keeps the current style. A repo file can still only
      tighten (existing `_conservative` rules), so a repo can make Light block.
- [ ] Background delivery under Light reuses the paths that already exist for non-blocking reviews (Phases 10, 14,
      15); a preset adds no new delivery. Per author: status line (claude); next-prompt context (`can_add_context`:
      claude, codex, and each new agent that the spike shows can add context); everyone: `watch`, the menu bar,
      `hooks.on_review`. Each new adapter declares these flags from its spike. An agent with none of the
      per-agent paths gets only the shared ones, and the summary says so for that agent.
- [ ] Tests: each preset writes exactly its table row (Standard removes the keys); detection on each row,
      absent and custom; custom is kept; `--yes` keeps the style; for each author, with no repo file, a Light run
      gives a background verdict, never a block. Separately: a repo `.tagteam.toml` setting `blocking.small = true`
      over a Light user config makes a small change block (the tighten-only rule still wins).

### 16e: Per-reviewer model prompt
- [ ] After the reviewer picker: one line per chosen reviewer with its current model/effort; Enter keeps it.
      List real choices where the CLI offers them (`cursor-agent --list-models`, `opencode models`).
      `--yes` and scripts skip it.

### Every unit
- [ ] Install and uninstall for each new agent use the existing contract: `base.save` (backup, temp file,
      re-parse, atomic replace), only tagteam-tagged entries changed, a re-run converges. No multi-file rollback
      (decided in Phase 11; a re-run fixes a partial install). New: a round-trip test per agent: install over a
      file that already has the user's own hooks, then uninstall, and the file is byte-identical (or absent if it
      was absent). For OpenCode, the user's other plugin files are untouched.
- [ ] Spike cleanup: logging hooks and plugins live in scratch repos (project-level files), never the user's
      global config. Each spike ends by checking that `~/.cursor/hooks.json`, `~/.config/opencode/plugins/`,
      `~/.kiro` and `~/.pi` hold nothing from the spike.
- [ ] Tests patch every new real path (`~/.cursor`, `~/.config/opencode/plugins`) per lessons.md; suite only via
      `python3 -m unittest discover -s tests -t .`; mutation checks in a scratch copy.

## Reviewer prompt tuning (2026-11-03)
Evidence: 125 past verdicts; median finding confidence 0.98, none under 0.6 (`min_confidence` never fires);
88 major / 21 blocker / 19 minor / 0 nit; 46% of reviews blocked.
- [x] runner.run_one saves `prompt.txt` per reviewer so future rubric changes can be replayed exactly
- [x] scripts/rubric_eval.py: recover past Copilot prompts from ~/.copilot/session-state, re-run old vs new
      rubric on the review-time snapshot (unreachable `tagteam-review` commit) or HEAD, report + labels.md
- [x] Replayed 10 past blocking cases. User labeled 14/15 recorded findings real. Stricter rubric (calibrated
      confidence, "minor at most without a named trigger", review-the-change) dropped real findings instead of
      downgrading them, and confidence stayed ~0.99. Reverted. For Copilot, the problem is recall/consistency (a
      re-run of the same old prompt re-found 5/13 real findings), not noise; agy and OpenCode were not replayed.
- [x] Intent: strip IDE context prefixes (ide_opened_file, ide_selection, browser_instruction, system-reminder);
      task-notification / agent-message turns keep the previous request (44/152 past intents were affected)
- [x] Read-only check false positives (all 4 reviewers disabled in math-quiz): .git/config compared by entry,
      branch.* tracking entries (VS Code, push -u) only warn; disable reasons and the log name the changed keys
      and sandbox files. The user suspected course/project hooks; checked: the only project hook is
      .claude/settings.json -> .claude/hooks/log_ai.py (prompt: writes a temp file; Stop, async: writes
      ai_log/*.md, then git add + commit). It never writes .git/config or .git/hooks; replaying its commit during
      a review on a copy changed neither, and the disabling runs logged no ref/tree change, so its commit had
      already finished. No installed .git/hooks, no core.hooksPath, no husky/lefthook/pre-commit, no
      .vscode/tasks.json. Both git-triggered disables logged `.git/config` only, never a hook file, so hook
      changes stay a hard disable (a hook is code that runs on the user's next git command). Root cause of the
      config change is still unknown; the next one is named in tagteam.log.
      Copilot's "wrote in its sandbox" disable (run 20261101T154350-b209) IS the project hook: Copilot runs
      Claude-format hooks from the sandbox's .claude/settings.json; its session log shows agentStop at 15:44:39,
      and the orphan commit 4f4c9a1 "ai_log: record session 2026-11-01_15-44-40_de821b24" carries that Copilot
      session's id. log_ai.py wrote ai_log/*.md in the sandbox and committed it there.
- [x] Reviewers don't run the reviewed repo's own hooks. Probed live (2026-11-03): a repo with hooks in
      .claude/settings{,.local}.json, .github/hooks, .codex/hooks.json, .gemini/, .agents/, run through each
      reviewer's real argv in a tagteam sandbox. From an untrusted folder only Claude ran them; from under
      ~/Coding (trusted by Copilot and Codex) Copilot ran both .claude/settings.json and .github/hooks too (trust
      follows the worktree's main repo). Codex, agy, OpenCode ran none. Fix: Claude `--setting-sources user`;
      every sandbox gets `.github/copilot/settings.local.json` with disableAllHooks (merged into an existing
      one, skipped if a symlink would take it outside), and dirty() ignores that file. Re-probe: no hook ran
      in any of the five, all exited 0. In-place reviews (no sandbox) still can run Copilot's repo hooks; Watch
      catches what they write. A repo's committed .github/copilot/settings.json with disableAllHooks false and
      inline hooks: hooks ran without the fix, none with it (local wins). A symlinked .github leaves them on.
- [x] Repo MCP servers (2026-11-04). Probe repo under ~/Coding with a server that writes a marker in .mcp.json,
      .github/mcp.json, .vscode/mcp.json, .codex/config.toml, .gemini/settings.json, .agents/mcp.json,
      .antigravity/mcp_config.json, opencode.json, plus .claude/settings.json enableAllProjectMcpServers.
      Only Copilot started one (.mcp.json; .github/mcp.json when there is no .mcp.json). Claude did too without
      --setting-sources user, none with it. `--disable-mcp-server '*'` is not a wildcard. Fix: the sandbox's
      settings.local.json also lists the servers from both files in `disabledMcpServers` (the key `copilot mcp
      disable` writes); re-probe with both files and with .github/mcp.json alone: none started.
- [ ] Not probed: plugins Copilot might load from a trusted repo (only user-installed plugins are documented).
- [x] Reviewer order agy, opencode, copilot, codex, claude; opencode pinned to free opencode/big-pickle
- [x] Reviewer order agy, copilot, opencode, codex, claude: a blocking round waits for its slowest reviewer (agy
      ~128s, opencode ~166s and 2/7 timeouts vs copilot ~30s), so copilot pairs with agy on large changes
- [x] A turn-end confirm round (round max+1) runs in the background, so the author no longer waits ~4 min. It still
      can't block: decide() never blocks past max rounds, so the worker carries only suggestions and sets no `owed`
      (test asserts owed None). Falls back to the foreground when the background can't start (no repo or snapshot,
      lock not held, no reviewers, spawn failed). The plan gate's confirm stays in the foreground: the user is
      waiting to approve that plan.
- [ ] Recall: consider 2 reviewers for small changes, or a second pass, now that agy/opencode are free
- [ ] Later: round-2 header says "Round 2" on round 3; prior findings JSON carries internal fields
