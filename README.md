# tagteam

When a coding agent finishes a turn that changed code, a design doc, or a plan, a *different* agent
reviews the change, read-only. For a large change, a plan or a design, the author waits for the review and
is told to fix any real problem before the turn ends. A small code change is reviewed in the background
instead, and its findings come back as suggestions. Works with Claude Code, Codex, Copilot CLI, and Antigravity (`agy`). Hooks fail open: if
tagteam breaks, your agent is never blocked.

Everything stays on your machine. tagteam calls the agent apps you already have installed and logged in;
it makes no calls to model providers of its own and sends nothing anywhere else.

Design notes: [DESIGN.md](DESIGN.md) and
[the packaging spec](docs/superpowers/specs/2026-10-26-packaging-design.md).

## Install

You need Python 3.11+, `git`, [`uv`](https://docs.astral.sh/uv/) (or `pipx`), and at least two of the
supported agents installed and logged in with your own account: one to write, one to review.

```sh
uv tool install git+https://github.com/azhou555/tagteam
# or, from a clone:  uv tool install ./tagteam
```

This puts a `tagteam` command on your PATH (`uv tool update-shell` if it isn't). Everything tagteam writes
into your agents' config calls that installed command by absolute path.

## First run

```sh
tagteam install
```

`install` first says what tagteam is and lists the agents it found (installed, credentials found, tagteam
hooks in place). It needs at least two. Then it asks, shows what it will change, and writes nothing until
you confirm:

1. **Consent.** It lists the agents that could review, each with the model it reviews with, and says that
   each one **receives your diffs**. You pick which may review and in what order (↑/↓, space, enter). Your
   saved list is preselected; picking none saves none. Then you pick **whose turns get reviewed** (every
   agent, unless you've changed it); an agent you leave out works unreviewed. This second picker shows only
   on a terminal; scripts use `--authors`.
2. **Summary.** One line per file: tagteam's hooks added, updated or already up to date, the Claude status
   line taken over (your own command still runs), the config keys it will save, and the menu bar item.
   `Apply? [Y/n]`. `--dry-run` prints the same summary plus a diff of each file, and stops.
3. **Hooks.** It adds tagteam's hooks to each agent's own config (`~/.claude/settings.json`,
   `~/.codex/hooks.json`, `~/.copilot/hooks/tagteam.json`, `~/.gemini/config/hooks.json`). It only merges:
   every entry it adds is tagged, your own hooks are untouched, and a backup is saved next to each file as
   `*.tagteam-bak-<time>` each time it writes one (the `updated` line names it). Each file is replaced in
   one step, so a failed write leaves it as it was. Re-running replaces only tagteam's own entries, so after
   any failure, fix the cause and run `tagteam install` again. It wires only the agents it finds installed;
   `--agents claude,codex` names them explicitly.
4. **Next steps.** Codex approval if needed, a first change to try, and `tagteam doctor`'s health check.

On a terminal each agent has its own color (claude orange, codex mint, copilot lavender, agy blue), what
will change is highlighted, and what stays as it is is dimmed. Set `NO_COLOR=1` for plain text; piped
output is always plain.

tagteam never sends code to an agent just because it is installed. The list is saved as `reviewers.use`
in `~/.tagteam/config.toml`, and **with no list, nothing is reviewed**: hooks run, turns are allowed, and
you see `⚠ Not reviewed: no reviewers configured; run tagteam install`.

Non-interactive installs (scripts, CI images) give the consent on the command line:

```sh
tagteam install --yes --reviewers codex,claude        # in preference order
tagteam install --yes                                 # no prompt or confirm; keeps your saved reviewer list
tagteam install --authors claude,codex                # limit whose turns get reviewed
tagteam install --no-menubar                          # leave SwiftBar and the Spotlight launcher alone
```

Two follow-ups:

- **Codex asks you to approve the hooks.** Open `codex`, type `/hooks`, and trust the three tagteam hooks.
  Codex will not run new or changed hooks until you do, and it asks again after any `tagteam install` that
  changes them.
- Run `tagteam doctor` (see [Doctor](#doctor)) to confirm everything is wired.

After that, work normally. A review runs when a turn ends with at least 5 changed lines (any change in later
rounds, or while earlier issues are still open). Findings arrive as a message starting with `[tagteam]`.
Reviewers also see the turn's git activity (new commits, rewritten history, pushes), but git activity alone
never starts a review.

Skip reviews with `TAGTEAM=off claude` (one session) or `touch <repo>/.tagteam-off` (one repo).

## Blocking and non-blocking reviews

Every change falls into one tier: `high_risk` (a path matched by `classify.risk`), `docs` (a plan or a
design doc), `large` (a code change of at least `review.large_lines` lines), or `small` (any other code
change). `review.blocking` says which tiers make the author wait:

```toml
[review]
blocking = { small = false, large = true, high_risk = true, docs = true }   # the defaults
```

- **Blocking review.** The turn ends only after the review. Issues at or above `review.block_at` are sent
  back to the author (`✗ Changes requested`), who fixes them or says why not, for up to
  `limits.max_rounds_per_turn` rounds plus a last confirm review.
- **Non-blocking review.** The turn ends at once with `⟳ Reviewing in the background; results appear in
  the status line`. The review runs on its own, against the tree the turn ended with, and spends none of the
  turn's rounds. Its issues become suggestions (`● Suggestions: N issues, not blocking`). At the start of the
  next prompt, Claude Code and Codex get the open suggestions as context labelled "not blocking; fix when
  convenient". Copilot and agy can't take added context at that point, so there the suggestions show only
  in the status line and the menu bar.
  Codex has no status line tagteam can use, so there the turn ends with `the result shows at your next
  prompt (live: tagteam watch)`, and your next prompt shows the result once as `↳ Hook · [tagteam] …`
  (a blocker instead stops the next turn, as above).

Three rules decide each issue's kind, in this order: a `blocker` always blocks, in any tier (a background
review's blocker blocks the next Stop as round 1, even if that turn changed nothing); an issue that was
already a suggestion stays one when it is re-checked; anything else takes the kind of the review that found
it. Once a blocking cycle is open, the reviews that re-check its fixes are blocking too, however small the
fix. The plan gate (Claude's `ExitPlanMode`) is always blocking.

Only one review of a session runs at a time. If a background review is still running when the next turn
ends, that turn waits for it, up to `review.deadline_s`. If it is still running then, the turn is not
reviewed (`⚠ Not reviewed: earlier review still running`), and the next turn's review covers both turns'
changes.

To make every change blocking, set `blocking = { small = true }` under `[review]` in
`~/.tagteam/config.toml`. A repo's `.tagteam.toml` may switch a tier to blocking, never back.

## What you'll see

Reviewers read your change in a **throwaway checkout**, not your working tree, and are run with their own
read-only flags. If a reviewer writes anywhere it shouldn't, the turn's notice says so, for example
`⚠ disabled codex: wrote outside read-only (tagteam doctor --clear)` or
`⚠ your repo changed during review (see tagteam.log)`.

Each review ends in one of these results. The same text and colors appear in the Claude status line, in
the notice at the end of the turn, in `tagteam watch` and `tagteam status`, and in the macOS menu bar.

| Result | Color | Meaning |
|---|---|---|
| `⟳ Reviewing changes` | peach | Reviewers are checking this turn now and the author is waiting; shows each reviewer's progress |
| `⟳ Reviewing in background` | peach | A non-blocking review is running; the author has moved on |
| `✓ Review passed` | mint | The review found nothing that must be fixed |
| `✗ Changes requested` | rose | Issues were sent back (`N issues to fix (round 1/2)`); the agent must fix them or say why not |
| `● Suggestions` | blue | Issues that don't block (`N issues, not blocking`); the agent is told at its next prompt |
| `✓ Issues resolved` | mint | An earlier block was settled: `all N fixed and re-checked`, or `changes reverted` |
| `⚠ Unresolved` | apricot | The last re-check still found issues after the fix rounds (`N issues still open after 2 fix rounds`); the turn is allowed |
| `⚠ Not reviewed` | yellow | No reviewer was available, no reviewers are configured, the review limit was reached, an earlier review was still running, the repo was too slow to sandbox, or tagteam hit a state error |
| `⚠ Review failed` | yellow | Every reviewer errored or timed out |
| `⚠ Review interrupted` | yellow | The review stopped before reviewers finished (`stopped before reviewers finished`), for example because its hook process was killed; open issues stay open |

Requested changes stay visible until a later review settles them, so you can tell whether issues were
fixed. If the follow-up review could not run, the notice adds `· N earlier issues not re-checked`.
Open issues carry over to the next prompt in the same session. A review that finds no blocking issues,
undoing the changes that raised them, or the last confirm review (`⚠ Unresolved`) closes them. Until then,
any change in a later turn is reviewed against them, however small. A failed, skipped or interrupted review
leaves them open.
The author can also answer with a rebuttal and no edits; that is re-reviewed too.

Each line also shows how long ago it happened, today's review count, and `⚠ N hook errors (24h)` when hooks
failed (reviews were skipped). In `tagteam watch` each agent's name is in its install color, and in the
menu bar each name has a dot in that color. Set `NO_COLOR=1` for plain text.

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
dropdown lists running reviews, open issues grouped as Blocking and Not blocking, counts, and recent
reviews with each one's kind; click one to open its run folder. If SwiftBar
has no plugin folder yet, `install` uses `~/SwiftBar`. `install` also adds `~/Applications/tagteam.app`, so
typing "tagteam" in Spotlight brings the item back. Without SwiftBar, or on Linux, the menu bar is skipped;
`--no-menubar` skips it too and leaves an existing item as it is.

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
| `review.blocking` | `small=false`, others `true` | Which tiers make the author wait (see [Blocking and non-blocking reviews](#blocking-and-non-blocking-reviews)) |
| `review.large_lines` | `150` | A code change this big counts as large |
| `review.block_at` | `major` | Lowest severity that blocks (`blocker`, `major`, `minor`) |
| `review.min_confidence` | `0.6` | Findings below this confidence are dropped |
| `review.deadline_s` | `240` | Time budget for a review, in seconds; time spent preparing the checkouts comes out of it |
| `review.max_diff_chars` | `60000` | Total diff text sent to reviewers |
| `review.ignore` | lockfiles, build output, minified files | Paths never sent for review |
| `reviewers.<agent>.model` | claude `sonnet`; others the CLI default | Model each reviewer runs |
| `reviewers.agy.effort` | `medium` | Thinking effort for `agy` |
| `limits.*` | see [Limits](#limits) | Loop stops |
| `routing.mode` | `ordered` | `ordered` takes the first available agents from `reviewers.use`; `quota` is for personal plans (below) |
| `classify.plan`, `.design`, `.risk` | glob lists | Which paths count as plans, design docs, or high-risk code |
| `hooks.on_review` | `""` (off) | Shell command run after each review (below) |

**Review notifications.** Set `hooks.on_review` to a shell command and tagteam starts it, detached, each
time a review is recorded, including skipped and interrupted ones. The command gets the run as JSON on
stdin, with two extra fields: `outcome` (`passed`, `blocked`, `resolved`, `unresolved`, `unreviewed`,
`failed`, `interrupted`, `suggestions`) and `summary` (the status-line text). The same two values are in the
environment as `TAGTEAM_OUTCOME` and `TAGTEAM_SUMMARY`, `TAGTEAM_MODE` is `block` or `advise` (the run's
kind), and `TAGTEAM_DEPTH` is set, so an agent the command starts is not
reviewed. tagteam never waits for the command, and a failure is only logged. Only `~/.tagteam/config.toml`
can set it. For example, a macOS notification:

```toml
[hooks]
on_review = 'osascript -e "display notification \"$TAGTEAM_SUMMARY\" with title \"tagteam\""'
```

**Quota mode.** If you are on personal plans and want to stretch their usage limits, set
`routing.mode = "quota"`. Reviewer choice within `reviewers.use` then follows each agent's remaining
headroom, tier, and reserve floor (see `[routing.quota]` in `tagteam config`). `reviewers.use` is still a
hard filter: an agent not listed never reviews, in either mode.

Old-shape config files (`[agents.*]`, `[routing] small/large/…`, `[review] max_rounds`) still load; their
keys are mapped to the new ones, and `install`, `doctor` and `tagteam config` log a deprecation line. An old `enabled = true` never counts as
consent.

### A repo's own file

A repo can carry its own `<repo>/.tagteam.toml`. tagteam treats it as untrusted, because anyone who
commits to the repo, including the agent being reviewed in the middle of a turn, can edit it. So it may
only **add** scrutiny: it can never reduce what gets reviewed, how strictly, or how often, and it never
affects other repos. Only these keys are read, each in one direction, against your own config and the
defaults:

| Key | A repo file may... |
|---|---|
| `authors.review`, `triggers.kinds` | add entries, not remove them |
| `triggers.min_changed_lines` | lower it |
| `review.block_at` | make it stricter (block on more severities, for example `minor`) |
| `review.min_confidence` | lower it |
| `review.per_change.small`, `.large`, `.high_risk`, `.docs` | raise them (more reviewers per change) |
| `review.blocking.small`, `.large`, `.high_risk`, `.docs` | switch them to `true` (make that tier blocking) |
| `review.large_lines` | lower it (minimum 1) |
| `classify.risk` | add patterns |
| `limits.max_rounds_per_turn`, `limits.max_reviews_per_hour` | lower them (minimum 1) |

Every other key in a repo file is ignored, and so is a value that moves the other way. That includes
`review.ignore` (adding patterns would hide code from review; your own list still applies),
`review.deadline_s`, `review.max_diff_chars`, `classify.plan`, `classify.design`, `limits.cooldown_min`,
`[reviewers]`, models, `[routing]` and `[hooks]`: which vendors see your code, and what runs on your
machine, is your choice, not a cloned repo's.
Each ignored key is listed in `tagteam.log` under `repo config ignored`.

`.tagteam.toml` can't turn reviews off, but two switches outside it do. A `<repo>/.tagteam-off` file turns
tagteam off for that repo, whoever created it: you, a commit you pulled, or an agent with shell access.
`TAGTEAM=off` in an agent's environment turns it off for that session.

## Limits

Nothing can review in an endless loop. Each stop is enforced in code and logged when it fires. The
defaults:

| Stop | Default | What happens |
|---|---|---|
| Reviewers don't trigger reviews | always | Reviewer processes carry `TAGTEAM_DEPTH`; hooks inside them do nothing |
| Rounds per turn | `limits.max_rounds_per_turn = 2` | After two blocking rounds, one last confirm review runs that cannot block; then the turn is always allowed |
| Reviews per hour | `limits.max_reviews_per_hour = 30` | Across all sessions and agents; past it you see `review limit reached (30 per hour)` and turns are allowed unreviewed. A crashed run still counts |
| No repeat reviews | always | A diff already approved is not reviewed again; a diff already blocked gets the same block re-issued without spending a review |
| One review at a time | always | A second trigger in the same session waits for the running review, up to `review.deadline_s`; past that, the turn is allowed with `⚠ Not reviewed: earlier review still running` and the next review covers it |
| Hook time budget | `review.deadline_s = 240` | Reviewers get what is left of `deadline_s` after setup (snapshots, checkouts). If setup alone takes over `min(60, deadline_s / 2)` seconds, the turn is allowed with `⚠ Not reviewed: repo too slow to sandbox, see tagteam.log`. The Stop hook timeout is `2 × deadline_s + 60`: one wait, then one review. A background review that overstays `deadline_s` by two minutes is stopped and shows as `⚠ Review interrupted` |
| Failure breaker | `limits.cooldown_min = 15` | A reviewer that errors twice in a row is paused for 15 minutes; if all are paused, the turn is allowed with `⚠ Not reviewed`. `tagteam doctor` shows paused reviewers and `tagteam doctor --clear` resumes them |

If tagteam's own state files can't be read or written, it skips the review and says
`⚠ Not reviewed: state error, see tagteam.log` rather than risk going past a limit.

There is no dollar cap. `tagteam status`, `watch` and the menu bar show this week's reviews per reviewer
and the cost the agents reported (`~$1.84 reported; 3 reviews didn't report cost`).

## Doctor

```sh
tagteam doctor             # read-only check, one line per agent
tagteam doctor --probe     # also runs one tiny real review per listed reviewer (this uses their quota)
tagteam doctor --clear     # re-enable reviewers disabled for writing, resume paused ones, then check
```

For each agent it shows whether it is installed, whether its hooks are present and point at a tagteam
that still exists, whether it is a listed reviewer (and its place in the order), whether it was disabled
for writing outside its read-only checkout or is paused after failures (with the minutes left), and, for
Codex, whether the `/hooks` approval is missing or stale. It does not check whether an agent is logged
in, because tagteam can't tell offline; `--probe` runs a real review, which needs a working login. If
tagteam's own list of disabled or paused reviewers can't be read, no reviewer runs until
`tagteam doctor --clear` resets it, and doctor says so. It exits 0 when everything it checked is healthy
and 1 otherwise, and prints the fix in plain words.

## Upgrading

**tagteam itself:** run the install command above again with `--reinstall`. The
hooks call the stable `tagteam` path, so they need no rewrite. Afterwards `tagteam doctor` shows anything
that needs attention. Coming from a version without background reviews, run `tagteam install` once more:
it raises the Stop hook timeout so a turn can wait for a background review (and in Codex,
re-approve its hooks if `tagteam doctor` says so).

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
- If you have a `~/.tagteam/config.toml` from before, your routing doesn't change: when it has no
  `routing.mode`, `install` writes `mode = "quota"` (with the bundled agent table and any `[agents.*]`
  tuning you had), so reviewers are still picked by usage headroom. Set `routing.mode = "ordered"` to use
  your list order instead. Without that file there is nothing to keep, so you get `ordered` routing, as
  new installs do. `tagteam config` shows which mode you have.
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
