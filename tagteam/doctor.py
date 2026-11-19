"""`tagteam doctor`: read-only health check per agent. Only `--clear` writes: it re-enables reviewers the sandbox
disabled and resumes ones paused after failures."""
import json
import math
import os
import re
import shlex
import shutil
import tempfile
import time

from . import changes, config, harnesses, health, install, limits, quota, runner, sandbox

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
        text = path.read_text()
    except OSError:
        return None
    try:
        cmds = _commands(json.loads(text))
    except ValueError:                               # a plugin file: its commands are JSON string literals
        cmds = [json.loads(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', text) if harnesses.base.TAG in s]
    out = []
    for c in cmds:
        try:
            argv = shlex.split(c.split(";", 1)[1])
            out.append(argv[:argv.index("hook")])
        except (IndexError, ValueError):
            out.append([c])
    return out


def author_part(h, entries):
    """(text, healthy) for the author role; healthy is True when the role isn't configured."""
    if not h.author:
        return None, True
    if not entries:
        return "author – no hooks", True
    missing = sorted({t for e in entries for t in e if t.startswith("/") and not os.path.exists(t)})
    if missing:
        return f"author ✗ hooks point at missing {', '.join(missing)} (run tagteam install)", False
    if any(e != install.entry_argv() for e in entries):
        return "author ✓ hooks ok · path differs from this install (tagteam install updates it)", True
    return "author ✓ hooks current", True


def reviewer_part(name, h, use, off, paused):
    if not h.reviewer:
        return None, True
    if name not in use:
        return "reviewer – not listed", True
    if off is None:
        return "reviewer ✗ disabled-reviewer list unreadable, so none review; `tagteam doctor --clear` resets it", False
    if name in off:
        return f"reviewer ✗ disabled by sandbox ({off[name].get('reason', '?')}); `tagteam doctor --clear` re-enables", False
    if paused is None:
        return "reviewer ✗ cooldown list unreadable, so none review; `tagteam doctor --clear` resets it", False
    if name in paused:
        return (f"reviewer ⚠ listed #{use.index(name) + 1}, paused {paused[name]} more min after failures; "
                "`tagteam doctor --clear` resumes"), True
    return f"reviewer ✓ listed #{use.index(name) + 1}", True


def paused_minutes(now):
    """{agent: whole minutes left} for reviewers in cooldown; None if the cooldown list can't be read."""
    c = quota.cooldowns()
    return None if c is None else {a: math.ceil((t - now) / 60) for a, t in c.items() if t > now}


def probe(name, cfg, repo=None, files=None, stale_s=7200):
    """One tiny real review in a sandbox checkout: (status, error text). status is 'ok', 'timeout', 'wrote-files',
    'failed' or 'unknown …'. With no `repo` it reviews a throwaway repo. With one (a real repo) it reviews a
    worktree of its HEAD plus `files` ({path: text}), built without touching its tree, index or refs; it never runs
    the reviewer in place there, and a change to the real repo (maybe the user's) is 'unknown'. No state is touched."""
    tmp = None
    try:
        if repo is None:
            repo = tmp = tempfile.mkdtemp(prefix="tagteam-probe-")
            for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
                changes.git(repo, *cmd)
            with open(os.path.join(repo, "a.py"), "w") as f:
                f.write("x = 1\n")
            changes.git(repo, "add", "-A")
            changes.git(repo, "commit", "-qm", "init")
        before = changes.snapshot(repo)
        tree = sandbox.overlay(repo, files) if files else before
        reviewer, options = harnesses.get(name).reviewer, cfg["reviewers"].get(name, {})
        watch = sandbox.Watch(repo, before)
        with sandbox.checkout(repo, tree, cwd=repo, stale_s=stale_s) as work, tempfile.TemporaryDirectory() as run_dir:
            if not tmp and os.path.realpath(work) == os.path.realpath(repo):
                return "unknown (no sandbox for the real repo; not run)", ""
            argv, stdin, out_file = reviewer.argv(PROBE_PROMPT, options, run_dir, PROBE_TIMEOUT_S, work)
            stdout, stderr, _, timed_out = runner._spawn(argv, stdin, work, PROBE_TIMEOUT_S)
            err = runner._why(stderr, stdout)
            after = watch.after()
            if after["git"] or after["outside"]:
                return "wrote-files", err
            if after["repo"]["tree"] or after["repo"]["refs"]:
                return ("wrote-files" if tmp else "unknown (the real repo changed during the probe)"), err
            try:
                if sandbox.written(work, tree):
                    return "wrote-files", err
            except (RuntimeError, OSError):
                return "unknown (could not re-check the checkout)", err
            if timed_out:
                return "timeout", err
            return ("ok", "") if reviewer.parse(stdout, out_file) is not None else ("failed", err)
    except (OSError, RuntimeError) as e:
        return f"failed ({e})", str(e)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def probe_one(name, cfg):
    """`probe`'s status alone: 'ok', 'timeout', 'wrote-files' or 'failed'."""
    return probe(name, cfg)[0]


def run(probe, clear):
    """Print one line per agent; return 0 if every configured role is healthy, else 1."""
    if clear:
        was, was_paused = limits.disabled(), paused_minutes(time.time())
        limits.clear_disabled()
        print("could not clear disabled reviewers (see tagteam.log)" if limits.disabled() != {}
              else "reset the unreadable disabled-reviewer list" if was is None
              else f"cleared disabled reviewers: {', '.join(was)}" if was else "no disabled reviewers to clear")
        if not limits.clear_pauses():                    # also resets failure counts, paused or not
            print("could not resume paused reviewers (see tagteam.log)")
        elif was_paused is None:
            print("reset the unreadable cooldown list")
        elif was_paused:
            print(f"resumed paused reviewers: {', '.join(was_paused)}")
    ok = True
    try:
        cfg = config.load(note_legacy=True)
    except config.ConfigError as e:
        print(f"config error: {e}")
        cfg, ok = None, False
    use = list(dict.fromkeys((cfg or {}).get("reviewers", {}).get("use") or []))
    off, paused, found = limits.disabled(), paused_minutes(time.time()), harnesses.detected()
    for name, h in harnesses.ALL.items():
        if name not in found:
            print(f"{name:9}not installed")
            continue
        parts, entries = [], hook_entries(h.hooks_path())
        for text, healthy in (author_part(h, entries), reviewer_part(name, h, use, off, paused)):
            if text:
                parts.append(text)
            ok &= healthy
        configured = bool(entries) or name in use
        if h.logged_in() is False and configured:
            parts.append("✗ not logged in")
            ok = False
        trust = h.hook_trust() if h.hook_trust and entries else "trusted"
        if trust != "trusted":
            parts.append({"unapproved": f"⚠ approve hooks in {name} (/hooks)",
                          "stale": f"⚠ re-approve hooks in {name} (/hooks)"}.get(trust, f"? approve hooks in {name} (/hooks)"))
        if (e := (health.load().get("reviewers") or {}).get(name)) and e.get("status", "ok") != "ok":
            probe_at = e.get("next_probe")
            parts.append(f"health {e['status']}" + (f" (next probe {time.strftime('%H:%M', time.localtime(probe_at))})"
                                                     if isinstance(probe_at, (int, float)) else ""))
        print(f"{name:9}" + " · ".join(parts))
    if probe:
        for name in use:
            if name in found and off is not None and name not in off and harnesses.get(name).reviewer:
                t0 = time.time()
                result = probe_one(name, cfg)
                print(f"probe {name}: {result} ({time.time() - t0:.0f}s)")
                ok &= result == "ok"
    return 0 if ok else 1
