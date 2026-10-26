
## Tests must isolate every real location, including ones read from the OS
- What happened: Install tests patched config file paths but not the SwiftBar folder, which install.py reads
  via `defaults read`. Each test run's `uninstall()` deleted the user's real menu bar plugin.
- Rule: any test that calls install/uninstall must patch every location those functions can resolve
  (constants *and* lookups like `defaults`, `which`, `~`). After adding a new install target, grep tests for
  `install.uninstall()` and check each one is isolated from it. Verify by running the suite with the real
  artifact present and checking it survives.
