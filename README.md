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
`[tagteam] ✓ Review passed: no issues found · copilot reviewed 13 changed lines in 7s`.

Reviewers also see the turn's git activity (new commits, rewritten history, pushes). Git activity alone
never starts a review.

**Claude status line.** Under your own status line, tagteam adds a line: `tagteam` in sage green, then the
latest status as pastel-colored text. It updates every 2s:

| Status | Meaning |
|---|---|
| peach `⟳ Reviewing changes` | Reviewers are checking this turn now; shows each reviewer's progress |
| mint `✓ Review passed` | The last review in this session found nothing that must be fixed |
| rose `✗ Changes requested` | The last review sent issues back; the agent must fix them or explain why not |
| yellow `⚠ Not reviewed` / `⚠ Review failed` | No reviewer had quota, or every reviewer errored |

The line also shows how long ago that was, today's review count, and `⚠ N hook errors (24h)` when hooks
failed. Hook errors mean reviews were skipped. Set `NO_COLOR=1` for plain text.

**Menu bar (macOS).** With [SwiftBar](https://github.com/swiftbar/SwiftBar) installed
(`brew install --cask swiftbar`), `install` adds a menu bar item that covers all four agents: a sage
two-person icon, then a status icon and today's review count (or the elapsed time while a review runs). The
dropdown lists running reviews, today's counts, and recent reviews: click one to open its run folder. If SwiftBar
has no plugin folder yet, `install` uses `~/SwiftBar` (SwiftBar ignores hidden folders). To start it at login,
turn on SwiftBar's own "Launch at Login" setting. `install` also adds `~/Applications/tagteam.app`, so you can
type "tagteam" in Spotlight to bring the menu bar item back after quitting SwiftBar.

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
