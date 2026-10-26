# tagteam

Cross-agent review hooks for Claude Code, Codex, Copilot CLI, and Antigravity (`agy`).
When any of them finishes a turn that changed code, a design doc, or a plan, the *other* agents
review the change read-only. If they find a major problem, the author is told to fix it before the
turn ends. Reviews go mostly to Copilot and Antigravity, which preserves your Claude and Codex usage.
Design and verification notes: [DESIGN.md](DESIGN.md).

## Install

```sh
./bin/tagteam install          # merge-only; backups saved next to each config as *.tagteam-bak-<time>
                               # re-run after updating tagteam: it refreshes the status line command
./bin/tagteam install --dry-run
```

Then **approve the hooks in Codex once**: run `codex`, type `/hooks`, and trust the three tagteam hooks.
Codex will not run new hooks until you do.

## Use

Nothing to do. Work normally in any of the four agents. A review runs when a turn ends with at least
5 changed lines (any change in later rounds). Findings arrive as a message starting with `[tagteam]`.
In Claude Code and Codex, a review that passes also prints one line, such as
`[tagteam] ✓ clean · copilot reviewed 13 changed lines in 7s`.

Reviewers also see the turn's git activity (new commits, rewritten history, pushes). Git activity alone
never starts a review.

In Claude Code, the status line gets a tagteam line under your own status line. It updates every 2s:

```
tagteam ⟳ reviewing changes 1/2 · copilot ✓ 11s · agy … 1m12s    while a review runs
tagteam ✓ clean (2 minor) · 4 reviews today                       last result in this session
tagteam ✗ 2 blocking · 4 reviews today · ⚠ 1 hook error (24h)     hook errors mean reviews were skipped
```

```sh
./bin/tagteam watch            # live view for a spare tab: running reviews, today's counts, last runs
./bin/tagteam status           # headroom per agent, review counts and share, recent runs
TAGTEAM=off claude             # skip reviews for one session
touch <repo>/.tagteam-off      # skip reviews in one repo
./bin/tagteam uninstall        # remove only tagteam's entries and restore your status line
```

Run artifacts (request, every reviewer's raw output, the verdict) are kept in `~/.tagteam/state/runs/<id>/`.
Errors go to `~/.tagteam/state/tagteam.log`. Hooks fail open: if tagteam breaks, your agent is not blocked.

## Configure

Copy any keys from `config.default.toml` into `~/.tagteam/config.toml` (all repos) or
`<repo>/.tagteam.toml` (one repo). Common changes:

| Key | Default | Meaning |
|---|---|---|
| `routing.primary_use` | `overflow` | Use Claude/Codex only when Copilot/agy can't cover the reviews (`never`, `balanced`) |
| `agents.<a>.quota.reserve_floor` | `0.5` | Never use a primary agent as reviewer below this much quota left |
| `routing.small` | `1` | Reviewers for small code changes (2 = fewer misses, double cost) |
| `review.block_at` | `major` | Lowest severity that blocks (`blocker` = less friction) |
| `review.deadline_s` | `240` | Per-reviewer time limit |
| `agents.copilot.quota.monthly_budget` | `200` | Fallback budget if the Copilot quota API is unavailable |

## Test

```sh
python3 -m unittest tests/test_tagteam.py
```
