
## Tests must isolate every real location, including ones read from the OS
- What happened: Install tests patched config file paths but not the SwiftBar folder, which install.py reads
  via `defaults read`. Each test run's `uninstall()` deleted the user's real menu bar plugin.
- Rule: any test that calls install/uninstall must patch every location those functions can resolve
  (constants *and* lookups like `defaults`, `which`, `~`). After adding a new install target, grep tests for
  `install.uninstall()` and check each one is isolated from it. Verify by running the suite with the real
  artifact present and checking it survives.

## Run the suite only with the documented command, and make any other way fail closed
- What happened (2026-10-28): I ran `python3 -m unittest -q` instead of `python3 -m unittest discover -s tests -t .`.
  Plain discovery imports `tagteam.harnesses` from the repo root before `tests/helpers.py` sets TAGTEAM_HOME,
  so `state.HOME` pointed at the real ~/.tagteam: the run replaced the user's config.toml (reviewers.use lost)
  and wrote fake runs, a fake disabled reviewer and a fake session into the live state.
- Rule: run tests only with the command in README "Development". A suite failure count far off the last
  baseline means the run itself is wrong: stop and find out why before running anything else.
- Encoded: tests/__init__.py raises if `tagteam.state` was imported before the tests, so the wrong command
  fails before any test writes.

## A mutation check must show it ran the tests before its result counts
- What happened (2026-10-28): I put two test names in `T="a b"` and ran `python3 -m unittest $T` in zsh.
  zsh doesn't word-split unquoted variables, so unittest got one bogus name and every run, the unmutated
  baseline included, reported `FAILED (errors=1)`. I first read that as "every mutation caught".
- Rule: run the unmutated baseline first and require OK; a mutation counts as caught only if it turns that OK
  into a *failure* (not an import/loader error), and the test count matches the baseline. Pass test names
  literally, not through a shell variable.
