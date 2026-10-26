"""`tagteam doctor`: read-only health check per agent. Only `--clear` writes (it re-enables sandbox-disabled reviewers)."""
import json
import os
import shlex
import shutil
import tempfile
import time

from . import changes, config, harnesses, install, limits, runner, sandbox

PROBE_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
PROBE_PROMPT = ("Review this one-line diff. Reply with only the JSON findings object; do not run commands "
                "or edit files.\n\n" + PROBE_DIFF)
PROBE_TIMEOUT_S = 120


def _commands(node):
    if isinstance(node, str):
        return [node] if harnesses.base.TAG in node else []
    items = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
    return [c for v in items for c in _commands(v)]


def hook_entries(path):
    """argv prefixes (the part before `hook`) of tagteam's hook commands in this agent's hook file.
    None if the file is missing or unreadable; [] if it holds none of ours."""
    try:
        cmds = _commands(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None
    out = []
    for c in cmds:
        try:
            argv = shlex.split(c.split(";", 1)[1])
            out.append(argv[:argv.index("hook")])
        except (IndexError, ValueError):
            out.append([c])
    return out


def author_part(name, h):
    """(text, healthy) for the author role; healthy is True when the role isn't configured."""
    if not h.author:
        return None, True
    entries = hook_entries(h.hooks_path())
    if not entries:
        return "author – no hooks", True
    missing = sorted({t for e in entries for t in e if t.startswith("/") and not os.path.exists(t)})
    if missing:
        return f"author ✗ hooks point at missing {', '.join(missing)} (run tagteam install)", False
    if any(e != install.entry_argv() for e in entries):
        return "author ✓ hooks ok · path differs from this install (tagteam install updates it)", True
    return "author ✓ hooks current", True


def reviewer_part(name, h, use, off):
    if not h.reviewer:
        return None, True
    if name not in use:
        return "reviewer – not listed", True
    if name in off:
        return f"reviewer ✗ disabled by sandbox ({off[name].get('reason', '?')}); `tagteam doctor --clear` re-enables", False
    return f"reviewer ✓ listed #{use.index(name) + 1}", True


def probe_one(name, cfg):
    """One tiny real review in a throwaway checkout: 'ok', 'timeout', 'wrote-files' or 'failed'. No state is touched."""
    repo = tempfile.mkdtemp(prefix="tagteam-probe-")
    try:
        for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            changes.git(repo, *cmd)
        with open(os.path.join(repo, "a.py"), "w") as f:
            f.write("x = 1\n")
        changes.git(repo, "add", "-A")
        changes.git(repo, "commit", "-qm", "init")
        tree = changes.snapshot(repo)
        reviewer, options = harnesses.get(name).reviewer, cfg["reviewers"].get(name, {})
        with sandbox.checkout(repo, tree) as work, tempfile.TemporaryDirectory() as run_dir:
            argv, stdin, out_file = reviewer.argv(PROBE_PROMPT, options, run_dir, PROBE_TIMEOUT_S)
            stdout, _, _, timed_out = runner._spawn(argv, stdin, work, PROBE_TIMEOUT_S)
            if sandbox.dirty(work, tree):
                return "wrote-files"
            if timed_out:
                return "timeout"
            return "ok" if reviewer.parse(stdout, out_file) is not None else "failed"
    except (OSError, RuntimeError) as e:
        return f"failed ({e})"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def run(probe, clear):
    """Print one line per agent; return 0 if every configured role is healthy, else 1."""
    if clear:
        limits.clear_disabled()
        print("cleared disabled reviewers")
    ok = True
    try:
        cfg = config.load()
    except config.ConfigError as e:
        print(f"config error: {e}")
        cfg, ok = None, False
    use = list(dict.fromkeys((cfg or {}).get("reviewers", {}).get("use") or []))
    off, found = limits.disabled(), harnesses.detected()
    for name, h in harnesses.ALL.items():
        if name not in found:
            print(f"{name:8}not installed")
            continue
        parts = []
        for text, healthy in (author_part(name, h), reviewer_part(name, h, use, off)):
            if text:
                parts.append(text)
            ok &= healthy
        configured = bool(hook_entries(h.hooks_path())) or name in use
        if h.logged_in() is False and configured:
            parts.append("✗ not logged in")
            ok = False
        trust = h.hook_trust() if h.hook_trust and hook_entries(h.hooks_path()) else True
        if trust is not True:
            parts.append(f"{'⚠' if trust is False else '?'} approve hooks in {name} (/hooks)")
        print(f"{name:8}" + " · ".join(parts))
    if probe:
        for name in use:
            if name in found and name not in off and harnesses.get(name).reviewer:
                t0 = time.time()
                result = probe_one(name, cfg)
                print(f"probe {name}: {result} ({time.time() - t0:.0f}s)")
                ok &= result == "ok"
    return 0 if ok else 1
