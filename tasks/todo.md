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
