# tagteam

When a coding agent finishes a turn that changed code, a design doc, or a plan, a *different* agent
reviews the change, read-only. If the reviewers find a real problem, the author is told to fix it before
the turn ends. Works with Claude Code, Codex, Copilot CLI, and Antigravity (`agy`). Hooks fail open: if
tagteam breaks, your agent is never blocked.

Everything stays on your machine. tagteam calls the agent apps you already have installed and logged in;
it makes no calls to model providers of its own and sends nothing anywhere else.

Design notes: [DESIGN.md](DESIGN.md) and
[the packaging spec](docs/superpowers/specs/2026-10-26-packaging-design.md).

## Install

You need Python 3.11+, `git`, [`uv`](https://docs.astral.sh/uv/) (or `pipx`), and at least two of the
supported agents installed and logged in with your own account: one to write, one to review.

```sh
uv tool install git+https://github.com/OWNER/tagteam     # OWNER: wherever your team hosts tagteam
# or, from a clone:  uv tool install ./tagteam
```

This puts a `tagteam` command on your PATH (`uv tool update-shell` if it isn't). Everything tagteam writes
into your agents' config calls that installed command by absolute path.

## First run

```sh
tagteam install
```

`install` does two things:

1. **Hooks.** It adds tagteam's hooks to each agent's own config (`~/.claude/settings.json`,
   `~/.codex/hooks.json`, `~/.copilot/hooks/tagteam.json`, `~/.gemini/config/hooks.json`). It only merges:
   every entry it adds is tagged, your own hooks and status line are untouched, a backup is saved next to
   each file as `*.tagteam-bak-<time>`, and re-running changes nothing new. It writes hooks for all four
   agents by default; `--agents claude,codex` limits that. `--dry-run` shows the changes without making them.
2. **Consent.** It lists the agents that could review and says that each one **receives your diffs**. You
   pick which may review and in what order. Nothing is preselected, and pressing Enter picks none.

tagteam never sends code to an agent just because it is installed. The list is saved as `reviewers.use`
in `~/.tagteam/config.toml`, and **with no list, nothing is reviewed**: hooks run, turns are allowed, and
you see `⚠ Not reviewed: no reviewers configured; run tagteam install`.

Non-interactive installs (scripts, CI images) give the consent on the command line:

```sh
tagteam install --yes --reviewers codex,claude        # in preference order
tagteam install --yes                                 # hooks only; saves no reviewer list, so no reviews
tagteam install --authors claude,codex                # limit whose turns get reviewed
```

Two follow-ups:

- **Codex asks you to approve the hooks.** Open `codex`, type `/hooks`, and trust the three tagteam hooks.
  Codex will not run new or changed hooks until you do, and it asks again after any `tagteam install` that
  rewrites them.
- Run `tagteam doctor` (see [Doctor](#doctor)) to confirm everything is wired.

After that, work normally. A review runs when a turn ends with at least 5 changed lines (any change in later
rounds). Findings arrive as a message starting with `[tagteam]`. Reviewers also see the turn's git activity
(new commits, rewritten history, pushes), but git activity alone never starts a review.

Skip reviews with `TAGTEAM=off claude` (one session) or `touch <repo>/.tagteam-off` (one repo).

## What you'll see

Reviewers read your change in a **throwaway checkout**, not your working tree, and are run with their own
read-only flags. If a reviewer writes anywhere it shouldn't, the turn's notice says so, for example
`⚠ disabled codex: wrote outside read-only (tagteam doctor --clear)` or
`⚠ your repo changed during review (see tagteam.log)`.

Each review ends in one of these results. The same text and colors appear in the Claude status line, in
the notice at the end of the turn, in `tagteam watch` and `tagteam status`, and in the macOS menu bar.

| Result | Color | Meaning |
|---|---|---|
| `⟳ Reviewing changes` | peach | Reviewers are checking this turn now; shows each reviewer's progress |
| `✓ Review passed` | mint | The review found nothing that must be fixed |
| `✗ Changes requested` | rose | Issues were sent back (`N issues to fix (round 1/2)`); the agent must fix them or say why not |
| `✓ Issues resolved` | mint | An earlier block was settled: `all N fixed and re-checked`, or `changes reverted` |
| `⚠ Unresolved` | apricot | The last re-check still found issues after the fix rounds (`N issues still open after 2 fix rounds`); the turn is allowed |
| `⚠ Not reviewed` | yellow | No reviewer was available, no reviewers are configured, the review limit was reached, or tagteam hit a state error |
| `⚠ Review failed` | yellow | Every reviewer errored or timed out |

Requested changes stay visible until a later review settles them, so you can tell whether issues were
fixed. If the follow-up review could not run, the notice adds `· N earlier issues not re-checked`.
The author can also answer with a rebuttal and no edits; that is re-reviewed too.

Each line also shows how long ago it happened, today's review count, and `⚠ N hook errors (24h)` when hooks
failed (reviews were skipped). Set `NO_COLOR=1` for plain text.

Commands:

```sh
tagteam watch      # live view for a spare terminal: running reviews, today's counts, last runs
tagteam status     # recent runs, per-reviewer counts, and this week's reviews and reported cost
tagteam config     # the effective config, and where each value came from (default, user, repo)
```

**Claude status line.** `install` wraps your existing Claude status line (yours still shows) and adds a
line under it: `tagteam` in sage green, then the latest result. It updates every 2 seconds. `uninstall`
restores your original.

**Menu bar (macOS).** With [SwiftBar](https://github.com/swiftbar/SwiftBar) installed
(`brew install --cask swiftbar`), `install` adds a menu bar item covering all four agents: a sage
two-person icon, a status icon, and today's review count (or the elapsed time while a review runs). The
dropdown lists running reviews, counts, and recent reviews; click one to open its run folder. If SwiftBar
has no plugin folder yet, `install` uses `~/SwiftBar`. `install` also adds `~/Applications/tagteam.app`, so
typing "tagteam" in Spotlight brings the item back. Without SwiftBar, or on Linux, the menu bar is skipped.

Run artifacts (the request, each reviewer's raw output, the verdict) are in `~/.tagteam/state/runs/<id>/`.
Errors go to `~/.tagteam/state/tagteam.log`. Set `TAGTEAM_HOME` to keep tagteam's data somewhere else.

## Configuration

Defaults work with no config beyond the reviewer list. Put your changes in `~/.tagteam/config.toml`.
`tagteam config` prints every key with its current value and source. Common keys:

| Key | Default | Meaning |
|---|---|---|
| `reviewers.use` | none | Agents that may review your code, in preference order. Set by `tagteam install` |
| `authors.review` | all four | Whose turns get reviewed |
| `triggers.kinds` | `["code", "plan", "design"]` | Which kinds of change start a review |
| `triggers.min_changed_lines` | `5` | Smaller code changes are skipped in round 1 |
| `review.per_change` | `small=1, large=2, high_risk=2, docs=2` | Reviewers per change |
| `review.large_lines` | `150` | A code change this big counts as large |
| `review.block_at` | `major` | Lowest severity that blocks (`blocker`, `major`, `minor`) |
| `review.min_confidence` | `0.6` | Findings below this confidence are dropped |
| `review.deadline_s` | `240` | Time limit per reviewer, in seconds |
| `review.max_diff_chars` | `60000` | Total diff text sent to reviewers |
| `review.ignore` | lockfiles, build output, minified files | Paths never sent for review |
| `reviewers.<agent>.model` | claude `sonnet`; others the CLI default | Model each reviewer runs |
| `reviewers.agy.effort` | `medium` | Thinking effort for `agy` |
| `limits.*` | see [Limits](#limits) | Loop stops |
| `routing.mode` | `ordered` | `ordered` takes the first available agents from `reviewers.use`; `quota` is for personal plans (below) |
| `classify.plan`, `.design`, `.risk` | glob lists | Which paths count as plans, design docs, or high-risk code |

A repo can carry its own `<repo>/.tagteam.toml`. It may set only `[triggers]`, `[authors]`, `[limits]`,
`[review]` and `[classify]`, and **it can only tighten**: lower limits, a longer cooldown, a smaller
`review.max_diff_chars`, and extra `review.ignore` patterns. It cannot raise a limit, remove an ignore
pattern, or set `[reviewers]`, models, or `[routing]`; which vendors see your code is your choice, not a
cloned repo's. Anything it sets outside that is dropped and noted in `tagteam.log`.

**Quota mode.** If you are on personal plans and want to stretch their usage limits, set
`routing.mode = "quota"`. Reviewer choice within `reviewers.use` then follows each agent's remaining
headroom, tier, and reserve floor (see `[routing.quota]` in `tagteam config`). `reviewers.use` is still a
hard filter: an agent not listed never reviews, in either mode.

Old-shape config files (`[agents.*]`, `[routing] small/large/…`, `[review] max_rounds`) still load; their
keys are mapped to the new ones and a deprecation line is logged. An old `enabled = true` never counts as
consent.

## Limits

Nothing can review in an endless loop. Each stop is enforced in code and logged when it fires. The
defaults:

| Stop | Default | What happens |
|---|---|---|
| Reviewers don't trigger reviews | always | Reviewer processes carry `TAGTEAM_DEPTH`; hooks inside them do nothing |
| Rounds per turn | `limits.max_rounds_per_turn = 2` | After two blocking rounds, one last confirm review runs that cannot block; then the turn is always allowed |
| Reviews per hour | `limits.max_reviews_per_hour = 30` | Across all sessions and agents; past it you see `review limit reached (30 per hour)` and turns are allowed unreviewed. A crashed run still counts |
| No repeat reviews | always | A diff already approved is not reviewed again; a diff already blocked gets the same block re-issued without spending a review |
| One review at a time | always | A second trigger in the same session while a review runs is skipped, not queued |
| Failure breaker | `limits.cooldown_min = 15` | A reviewer that errors twice in a row is paused for 15 minutes; if all are paused, the turn is allowed with `⚠ Not reviewed` |

If tagteam's own state files can't be read or written, it skips the review and says
`⚠ Not reviewed: state error, see tagteam.log` rather than risk going past a limit.

There is no dollar cap. `tagteam status`, `watch` and the menu bar show this week's reviews per reviewer
and the cost the agents reported (`~$1.84 reported; 3 reviews didn't report cost`).

## Doctor

```sh
tagteam doctor             # read-only check, one line per agent
tagteam doctor --probe     # also runs one tiny real review per listed reviewer (this uses their quota)
tagteam doctor --clear     # re-enable reviewers tagteam disabled for writing, then check
```

For each agent it shows whether it is installed and logged in, whether its hooks are present and point at
a tagteam that still exists, whether it is a listed reviewer (and its place in the order), whether it was
disabled for writing outside its read-only checkout, and, for Codex, whether the `/hooks` approval is
missing or stale. It exits 0 when everything it checked is healthy and 1 otherwise, and prints the fix in
plain words.

## Upgrading

**tagteam itself:** reinstall with `uv tool install --reinstall git+https://github.com/OWNER/tagteam`. The
hooks call the stable `tagteam` path, so they need no rewrite. Afterwards `tagteam doctor` shows anything
that needs attention.

**From an older checkout-based install** (hooks that run `<repo>/bin/tagteam`):

```sh
cd path/to/tagteam
uv tool install -e .       # editable: the hooks now run this working copy
tagteam install            # rewrites the old hook entries and asks you to confirm reviewers
tagteam doctor
```

- `install` finds the old entries by their tag, whatever path they point to. Before rewriting each agent's
  entries it runs that agent's bundled sample payload through the new command; if that check fails, the old
  entries stay and `install` says which agent and why.
- **Reviews pause until you confirm reviewers.** An old config's `enabled` flags are never read as consent. The
  prompt preselects the agents you had enabled; type their numbers to keep them. Until then turns show
  `⚠ Not reviewed: no reviewers configured; run tagteam install`.
- Routing is `ordered` unless you set `routing.mode = "quota"` in `~/.tagteam/config.toml`. Add that line to
  keep routing by usage headroom.
- Re-approve the Codex hooks in `/hooks` if `doctor` asks.

## Uninstall

```sh
tagteam uninstall          # removes only tagteam's hook entries, restores your status line,
                           # and removes the menu bar plugin and launcher it added
uv tool uninstall tagteam
```

`~/.tagteam` (config, run history, logs) is left in place; delete it if you want it gone. Your agents'
own settings are otherwise untouched, and the backups `install` made are still next to them.

## Development

```sh
python3 -m unittest discover -s tests -t .     # needs Python 3.11+; tests never touch real agent config
bash scripts/clean_install_test.sh             # installs HEAD into a throwaway HOME and checks the lifecycle
```

The clean-install script sets `TAGTEAM_NO_MACOS_EXTRAS=1`, which makes `install` and `uninstall` leave
SwiftBar, the launcher and the Claude status line alone. On Linux you can run it in a container:
`docker run --rm -v "$PWD":/src -w /src python:3.12 bash scripts/clean_install_test.sh`.
