# Self-healing tagteam

## Intent

tagteam runs in the background between coding agents. When it breaks, nobody notices: Copilot sat disabled for 15
days, agy and opencode timed out on most turns, and 24 of 91 runs (Nov 4–18) went fully unreviewed. The user wants
tagteam to be **as self-healing as possible**: resolve issues behind the scenes and involve the user only when
nothing else will work. The goal is that it cooperates with agents autonomously.

## Goals

1. Fewer unreviewed turns. When a reviewer fails, another healthy reviewer covers the turn when there's time. A
   reviewer that keeps failing stops being picked until it's healthy again.
2. Breakers recover on their own. Today, failure pauses expire but nothing checks the reviewer is fixed, and a
   reviewer disabled for writing stays off until `doctor --clear`.
3. Housekeeping happens unattended: old run folders and stale sandbox worktrees.
4. The user hears about only what tagteam can't fix, in the places they already look (Claude status line, menu
   bar, end-of-turn notice), with the exact fix.
5. What tagteam fixed on its own is visible on request (`watch`, menu bar, `doctor`), never pushed.

## Non-goals

- Changing why agy times out. That is a separate investigation. This design routes around it.
- Hedging (starting a backup reviewer while the first is still running). It doubles spend on slow reviewers. Revisit
  if goal 1 isn't met after a week of data.
- A long-running daemon or launchd job.
- Reordering `reviewers.use` permanently. The user just set it (`codex, agy, copilot, claude`; opencode off).

## Anti-goals (failure even if it works)

- A healer that weakens the write-safety breaker: a reviewer that wrote to the real tree must not come back unless a
  test proves it no longer writes.
- Healing that adds visible latency to a turn. Probes never run on a turn's critical path.
- Notification noise. Healthy, or healed, means silence in the status line and notices.
- Spending surprises. Probes cost real requests (Copilot premium requests) and must be rationed.
- Sending code to a reviewer outside `reviewers.use`.

## Approaches considered

1. **Heal inside each review.** Every check runs inline in the hooks. Nothing new to install, but probes (5–40s) would
   land on turns.
2. **Background healer (launchd).** Periodic probe and prune, writing one health file. Clean split, but a new moving
   part to install, keep alive and uninstall.
3. **Hybrid (chosen).** The fallback runs inside the review, where turns go unreviewed. The slow work (probes,
   re-enabling, pruning) runs in a detached tick that a hook starts at most every ~15 minutes, reusing the existing
   detach machinery. Nothing new to install.

The user chose 3, including the re-enable safety rule below.

## Evidence that shapes the design

- In the 24 fully-unreviewed runs, 38 reviewer calls failed: **28 timeouts**, 7 fast failures (<120s), 3 slow
  failures. A timeout consumes the deadline, so in-run fallback saves at most the 7 runs with a fast failure.
  **Demotion is the main lever; fallback is secondary.**
- Breakers today: `limits.note_result` (2 failures in a row → `quota.cooldown(cooldown_min)`), quota cooldowns
  (`retryAfterMs`-aware, as of PR #6), and `limits.disable` (wrote outside read-only; manual `doctor --clear` only).
- `live.outcome` already labels an all-failed run "failed". A partial failure shows as passed with
  `· unavailable: X` in the completion line. That is fine and stays.
- `runs/` holds 273 dirs (9.8 MB). `locks/` holds 205 files; see §3.4 for why they stay.

## Design

### 1. Health state (`state/health.json`, the single source)

Per reviewer: `{status, since, reason, next_probe, probe_backoff_min}` where `status` is one of:

| status | meaning | enforced by | cleared by |
|---|---|---|---|
| `ok` | eligible | | |
| `demoted` | recent calls mostly failed | router sorts it after every `ok` reviewer | any success, real or probe |
| `paused` | 2-failure or quota cooldown | existing `cooldowns.json` (unchanged) | time alone, as today |
| `disabled` | wrote outside read-only | existing `disabled.json` (unchanged); history in `trips.json` | a passing canary, if not latched (§3.3) |
| `needs_you` | tagteam can't fix it (list below) | nothing new: display only | the fix is detected on a tick |

`disabled.json`, `cooldowns.json` and the new `trips.json` are the safety files, all read fail-closed. The router reads only the first two, unchanged; `trips.json` is read by `limits.disable` and the tick.
An unreadable `health.json` means everyone is `ok`, because it is advisory, unlike the safety files.

**Paused is purely time-based (deliberate).** At expiry the reviewer is eligible again with no probe, as today. A
second failure re-pauses it through the existing 2-failure breaker, and repeated failure demotes it. A recovery
gate would hold a reviewer out until a tick happens to probe it, which costs coverage for no safety gain: these
pauses protect quota and latency, not the tree.

**`needs_you` is display only.** Its conditions: CLI missing, not logged in, an auth error on a probe, the write
breaker tripped twice (§3.3), stale hooks, or no listed reviewer can run at all. It never excludes a reviewer from
routing, so a flaky `logged_in()` check can't silence a working one. A reviewer that really can't log in fails, and
then gets demoted or disabled by the existing mechanisms.

**Demotion rule (builder may tune).** Computed from `history.jsonl`. A reviewer is demoted when at least 3 of its
last 4 calls failed (timeouts count) **and** its latest call failed. Probe results go to a separate `probes.jsonl`, so they never count toward load, spend or `status` tallies, and
demotion reads both files. A passing probe or any real success clears demotion at once.
- `ordered` mode (the default, and what the user runs): keep the user's order, but stable-sort `demoted` after `ok`.
- `quota` mode: drop demoted reviewers from `eligible()`'s pool unless the pool would otherwise have fewer than the
  needed count.
- Either way, a demoted reviewer still reviews when nothing healthier is available. Some review beats none.

### 2. In-run fallback (runner/core)

When a planned reviewer fails without timing out and at least 90s of deadline remain, start the next candidate. A
candidate is a listed reviewer, not the author, not already in the run, `ok` or `demoted`, and not cooling or
disabled, and `ok` before `demoted`. It takes over the failed reviewer's whole task list (kinds, lenses,
artifacts). Its prompt is rebuilt for its name, with the same prior findings and author reply. It gets its own fresh
checkout (`sandbox.checkout`) and the remaining deadline.

Each failed reviewer gets at most one fallback, and a fallback that fails gets none (no chains). Fallback applies in
every mode and round, including confirm and background reviews. The write check (`sandbox.dirty` and `Watch`) covers
the fallback's checkout like any other. The run record lists the fallback under `reviewers` with
`fallback_for: {new: failed}`, so `status` and `watch` can show it.

### 3. The heal tick (`tagteam heal`, detached)

Started by any hook (turn-start or turn-end) through the existing detach path when
`now - health.json.last_tick >= 15 min`, under a non-blocking lock so only one runs. `doctor --fix` runs it in the
foreground. Each tick:

1. **Detect needs-you conditions** cheaply: CLI on PATH, `logged_in()`. If one is now fixed, flip back to `ok` and
   log it.
2. **Probe** each `demoted` reviewer when its `next_probe` is due. Paused reviewers are not probed (see §1). The probe
   is `doctor.probe_one`, extended to also return the error text; `doctor`'s own output stays the same. The probe's
   result is appended to `probes.jsonl`.
   - Pass: the demotion clears (§1).
   - Error matching an auth pattern (`unauthori[sz]ed|not logged in|log ?in|\b40[13]\b|authenticat|credentials`):
     `needs_you`.
   - Any other failure: double `probe_backoff_min` (15 → 30 → … capped at 360).
   - At most 2 probes per tick. Copilot probes are capped at 4 a day (premium requests).
3. **Canary-probe disabled reviewers.** The write-safety breaker is a small state machine over two safety files. Both
   are read fail-closed, the same way `disabled.json` is read today, and neither is ever advisory:
   - `disabled.json` (unchanged): who is disabled **now**.
   - `trips.json` (new): `{reviewer: {"trips": [ts, …], "latched": bool}}`, the breaker's **history**. A canary never
     deletes from it. If it is unreadable, no canary runs and every disabled reviewer stays disabled.

   `limits.disable` appends a trip each time it fires. If the reviewer already has another trip within the 7 days
   before this one, it also sets `latched: true`. Only `doctor --clear` resets `latched`. Trip timestamps are kept as
   history; age only decides whether two trips are "within 7 days" of each other.

   - **Migration:** an existing `disabled.json` entry with no `trips.json` record (Copilot's today) counts as one trip
     at the entry's `ts`, unlatched.
   - **Canary-eligible:** in `disabled.json` and not latched, **however old the trip is**. A tick runs it on a throwaway
     repo seeded with every write trap seen so far: a `.claude/settings.json` Stop hook and a `.github/hooks` hook that
     each write a marker file, plus a `.mcp.json` server that writes a marker when it starts. The run goes through the
     normal sandbox path on a tiny diff.
   - **Pass, only if all three hold:** the reviewer returned a valid parsed review (not a timeout, a startup error or
     unparsable output), no marker appeared, and `sandbox.dirty` and `Watch` are clean. Then the `disabled.json` entry
     is removed and the reviewer is `ok`. Its `trips.json` history stays.
   - **Inconclusive** (timeout, error, invalid output, or a check that couldn't run): it stays disabled, and the next
     canary backs off like a probe.
   - **Wrote in the canary:** this counts as a trip, recorded with the canary's run id. It stays disabled, is latched,
     and becomes `needs_you`.
   - **Latched:** it stays disabled and shows `needs_you` with no more canaries until `doctor --clear`, which also
     clears its trips.
4. **Prune:** run dirs older than 30 days, and stale sandbox worktrees (reuse `sandbox._clear_stale`). **Lock files
   are never deleted.** Openers take `flock` after `open()`, so unlinking a lock lets an old-inode holder and a
   new-file holder both in. There are 205 lock files, each a few bytes.
5. **Write `health.json`** and append each action to `state/heal.jsonl`:
   `{ts, reviewer, action: "re-enabled"|"demoted"|"restored"|"pruned"|"needs_you", why}`.

A failed tick logs the failure and never raises into a hook.

### 4. What the user sees

- **Healthy or healed: nothing new** in the status line, menu bar title, or notices.
- **`needs_you`:** one segment in the Claude status line and the menu bar title, e.g.
  `⚠ copilot: not logged in → copilot login`. The end-of-turn notice carries it at most once per 24h per issue.
  "All listed reviewers unavailable" is also `needs_you` (no reviewer can run at all).
- **`watch` and the menu bar dropdown** gain a "Health" block: each reviewer's status, plus the last 5 `heal.jsonl`
  entries ("10:42 re-enabled copilot: canary clean").
- **`doctor`** reads `health.json` as well as running its checks, and shows each reviewer's status and next probe.
  `doctor --fix` runs a tick now. `doctor --clear` stays as the manual override. Fix the column width bug
  (`opencodeauthor`).
- **`status`** shows fallbacks, e.g. `codex (for agy)`.

## Calls I made (the user can override)

- Thresholds: demote at ≥3 of the last 4 calls failed; 90s minimum for a fallback; 15-minute tick; probe backoff
  capped at 6h; at most 4 Copilot probes a day; prune run dirs at 30 days.
- Lock files are not pruned (unsafe, see §3.4). Paused reviewers come back on time alone (§1).
- `needs_you` is display only, never a routing exclusion (§1).
- tagteam does **not** rewrite agents' hook config when it is stale. That is `install`, which asks before writing.
  Stale hooks become a `needs_you` item: `→ tagteam install`.
- Quota cooldowns aren't probed early. The provider said when to come back.

## Left to the builder

- Where demotion is computed: in the router from `history.jsonl`, or kept in `health.json` by the runner.
- The canary repo's exact layout, and how its markers are detected.
- Exact status-line and menu text, within the existing `live.Paint` and SwiftBar conventions.
- Test layout. Follow `tasks/lessons.md`: run the suite only with `python3 -m unittest discover -s tests -t .`, and
  mutation-check against scratch copies.
