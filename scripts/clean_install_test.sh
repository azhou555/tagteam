#!/usr/bin/env bash
# Clean-machine install test: installs the committed HEAD as a uv tool into a throwaway HOME, wires fake
# agents, and checks install, doctor and uninstall. Touches nothing outside its temp dir.
# Run from the repo root: bash scripts/clean_install_test.sh   (Linux: docker run --rm -v "$PWD":/src -w /src python:3.12 bash scripts/clean_install_test.sh)
set -euo pipefail

SRC=$PWD
[ -f "$SRC/pyproject.toml" ] || { echo "run from the repo root" >&2; exit 2; }
fail() { echo "clean install: FAILED: $*" >&2; exit 1; }

if ! command -v uv >/dev/null 2>&1; then
    python3 -m pip install --quiet --user uv 2>/dev/null || python3 -m pip install --quiet uv
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || fail "uv is not available after pip install"
fi
[ -z "$(git -C "$SRC" status --porcelain 2>/dev/null)" ] || echo "note: uncommitted changes are not tested; uv installs the committed HEAD"

T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export HOME="$T/home" TAGTEAM_HOME="$T/home/.tagteam" UV_TOOL_DIR="$T/uv-tools" UV_TOOL_BIN_DIR="$T/uv-bin"
export XDG_CONFIG_HOME="$HOME/.config" XDG_DATA_HOME="$HOME/.local/share" XDG_CACHE_HOME="$HOME/.cache" XDG_STATE_HOME="$HOME/.local/state"
export TAGTEAM_NO_MACOS_EXTRAS=1                      # no SwiftBar, launcher or status line, even on a Mac
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*'   # a bind-mounted repo has another owner
unset TAGTEAM TAGTEAM_DEPTH CODEX_HOME
mkdir -p "$HOME" "$T/fakebin" "$HOME/.claude" "$HOME/.codex"

for agent in claude codex; do                         # stand-ins so install detects the agents
    printf '#!/bin/sh\nexit 0\n' > "$T/fakebin/$agent"
    chmod +x "$T/fakebin/$agent"
done
export PATH="$T/fakebin:$UV_TOOL_BIN_DIR:$PATH"

printf '{\n  "theme": "dark",\n  "hooks": {}\n}\n' > "$HOME/.claude/settings.json"     # in the layout tagteam writes, so a restore compares equal
printf '{\n  "hooks": {}\n}\n' > "$HOME/.codex/hooks.json"
mkdir -p "$T/before"
cp "$HOME/.claude/settings.json" "$T/before/claude.json"
cp "$HOME/.codex/hooks.json" "$T/before/codex.json"

uv tool install --quiet "git+file://$SRC@$(git -C "$SRC" rev-parse HEAD)" || fail "uv tool install"   # by commit: a CI checkout is a detached HEAD with no default branch
ENTRY="$UV_TOOL_BIN_DIR/tagteam"
[ -x "$ENTRY" ] || fail "no tagteam executable at $ENTRY"
[ "$(command -v tagteam)" = "$ENTRY" ] || fail "tagteam on PATH is not the installed one"

tagteam install --yes --agents claude,codex --reviewers codex | tee "$T/install.out"
grep -q "menu bar: skipped" "$T/install.out" || fail "install did not skip the menu bar"

grep -q "$ENTRY" "$HOME/.claude/settings.json" || fail "claude hooks do not use $ENTRY"
grep -q "$ENTRY" "$HOME/.codex/hooks.json" || fail "codex hooks do not use $ENTRY"
grep -q '^use = \["codex"\]' "$TAGTEAM_HOME/config.toml" || fail "config.toml lacks use = [\"codex\"]"

set +e
tagteam doctor > "$T/doctor.out"
code=$?
set -e
cat "$T/doctor.out"
[ "$code" = 0 ] || fail "doctor exited $code, expected 0"
grep -q "reviewer ✓ listed #1" "$T/doctor.out" || fail "doctor does not list codex as reviewer #1"

tagteam uninstall
cmp "$T/before/claude.json" "$HOME/.claude/settings.json" || fail "claude settings changed by install + uninstall"
cmp "$T/before/codex.json" "$HOME/.codex/hooks.json" || fail "codex hooks changed by install + uninstall"

echo "clean install: ok"
