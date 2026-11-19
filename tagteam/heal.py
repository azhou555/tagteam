"""The self-heal tick, detached from a hook at most every TICK_S: probe demoted reviewers, canary disabled ones,
prune old run dirs and stale worktrees, and record in health.json (and heal.jsonl) what needs the user."""
import contextlib
import datetime
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from . import doctor, harnesses, health, limits, quota, router, sandbox, state

TICK_S = 900
AUTH_ERR = re.compile(r"unauthori[sz]ed|not logged in|log ?in|\b40[13]\b|authenticat|credentials", re.I)
MAX_PROBES = 2                  # per tick, canaries included: each is a real request
COPILOT_PER_DAY = 4             # premium requests
BACKOFF_MIN, BACKOFF_MAX = 15, 360
KEEP_RUNS_S = 30 * 86400
RECENT_S = 7 * 86400
CLEAR_STATE = ("next_probe", "probe_backoff_min", "auth")


def _try_lock(name):
    """The open file holding `locks/<name>.lock`, or None while another process (or thread) holds it."""
    f = open(state.path("locks", f"{name}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError as e:
        f.close()
        if isinstance(e, BlockingIOError):
            return None
        raise


def _launch():
    subprocess.Popen([sys.executable, "-m", "tagteam", "heal"],
                     cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),     # finds this tagteam
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def maybe_start(now=None):
    """Start a detached tick if TICK_S passed since the last one; True if this call started it. Never raises."""
    try:
        now = time.time() if now is None else now
        f = _try_lock("heal")
        if f is None:
            return False
        with f:
            h = health.load()
            last = h.get("last_tick")
            if isinstance(last, (int, float)) and now - last < TICK_S:
                return False
            health.save({**h, "last_tick": now})
        _launch()
        return True
    except Exception as e:
        with contextlib.suppress(Exception):
            state.log("heal tick not started", error=repr(e))
        return False


def _section(d, key):
    s = d.get(key)
    return {k: dict(v) for k, v in s.items() if isinstance(v, dict)} if isinstance(s, dict) else {}


def tick(cfg, now=None):
    """One pass of spec §3's steps, in order; the events it logged, or None if another tick holds the lock."""
    f = _try_lock("heal-run")
    if f is None:
        return None
    with f:
        now = time.time() if now is None else now
        old = health.load()
        revs, authors = _section(old, "reviewers"), _section(old, "authors")
        use = [n for n in dict.fromkeys(cfg["reviewers"].get("use") or []) if n in harnesses.ALL]
        for n in use:
            revs.setdefault(n, {})
        events, why = [], {}

        def note(who, action, reason):
            health.event(who, action, reason)
            events.append({"reviewer": who, "action": action, "why": reason})
        found = harnesses.detected()
        _refresh(use, revs, found, now, note, why)                          # 1. needs-you and status changes
        budget = [MAX_PROBES]
        for step in (_probe_demoted, _canary_disabled):                     # 2. probes, 3. canaries
            try:
                step(cfg, use, revs, found, now, budget, why)
            except Exception as e:
                state.log("heal step failed", step=step.__name__, error=repr(e))
        try:                                                                # 4. prune
            if gone := _prune(cfg, now):
                note(None, "pruned", f"{gone} run dirs older than 30 days")
        except Exception as e:
            state.log("heal prune failed", error=repr(e))
        _refresh(use, revs, found, now, note, why)                          # 5. record
        _authors(cfg, found, authors, now, note)
        cur = health.load()
        last = cur.get("last_tick")
        health.save({**cur, "reviewers": {n: revs[n] for n in use}, "authors": authors,
                     "last_tick": max(now, last) if isinstance(last, (int, float)) else now})
        return events


def _status(name, e, found, off, eligible, trips_ok):
    """(status, reason) from the cheap checks and the safety files; needs_you reasons end with the fix."""
    if name not in found:
        return "needs_you", f"{name} not found on PATH → install {name}"
    if harnesses.ALL[name].logged_in() is False:
        return "needs_you", f"not logged in → log in to {name}"
    if off is None or name in off:
        if off is not None and name in eligible:
            return "disabled", "wrote outside read-only; a clean canary re-enables it"
        if off is None or not trips_ok:
            return "needs_you", "safety state unreadable → tagteam doctor --clear"
        return "needs_you", "wrote outside read-only again → check it, then tagteam doctor --clear"
    if quota.cooling(name):
        return "paused", "cooling down after failures or a quota limit"
    if health.is_demoted(name):
        return ("needs_you", e["auth"]) if e.get("auth") else ("demoted", "most recent calls failed")
    return "ok", ""


def _refresh(use, revs, found, now, note, why):
    off, eligible, trips_ok = limits.disabled(), limits.canary_eligible(), limits.trips() is not None
    for n in use:
        e = revs[n]
        status, reason = _status(n, e, found, off, eligible, trips_ok)
        _transition(n, e, status, reason, now, note, why.pop(n, None))
        if status == "ok":
            for k in CLEAR_STATE:
                e.pop(k, None)


def _transition(who, e, status, reason, now, note, cause=None):
    was = e.get("status", "ok")
    if status != was or status == "needs_you" and e.get("reason") != reason:
        if status == "ok" and was in ("demoted", "needs_you", "disabled"):
            note(who, "re-enabled" if was == "disabled" else "restored", cause or f"cleared: {e.get('reason') or was}")
        elif status in ("demoted", "needs_you"):
            note(who, status, reason)
        if status != was:
            e["since"] = now
    e.update(status=status, reason=reason)
    e.setdefault("since", now)


def _authors(cfg, found, authors, now, note):
    """needs_you per author agent (a namespace apart from reviewers): stale hooks, or no listed reviewer can run."""
    flagged = {}
    for name, h in harnesses.ALL.items():
        if name in found and h.author:
            entries = doctor.hook_entries(h.hooks_path())
            why = [] if doctor.author_part(h, entries)[1] else ["stale hooks → tagteam install"]
            if entries and name in cfg["authors"]["review"] and not router.candidates(name, cfg):
                why.append("no listed reviewer can run → tagteam doctor" if cfg["reviewers"].get("use")
                           else "no reviewers listed → tagteam install")
            if why:
                flagged[name] = "; ".join(why)
    for name in set(authors) | set(flagged):
        e = authors.setdefault(name, {})
        _transition(name, e, "needs_you" if name in flagged else "ok", flagged.get(name, ""), now, note)
        if name not in flagged:
            del authors[name]


def _back_off(e, now):
    b = e.get("probe_backoff_min")
    b = min(BACKOFF_MAX, b * 2) if isinstance(b, (int, float)) and b > 0 else BACKOFF_MIN
    e.update(probe_backoff_min=b, next_probe=now + b * 60)


def _may_probe(name, e, found, now, budget):
    t = e.get("next_probe")
    if isinstance(t, (int, float)) and t > now or budget[0] <= 0 or name not in found:
        return False
    if not harnesses.ALL[name].reviewer:
        return False
    if name == "copilot":
        midnight = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = state.read_jsonl(state.path("probes.jsonl"), midnight.timestamp())
        if sum(r.get("reviewer") == name for r in rows) >= COPILOT_PER_DAY:
            return False
    budget[0] -= 1
    return True


def _probe_demoted(cfg, use, revs, found, now, budget, why):
    off = limits.disabled()
    for n in use:
        e = revs[n]
        if off is None or n in off or quota.cooling(n) or harnesses.ALL[n].logged_in() is False \
                or not health.is_demoted(n):
            continue
        if not _may_probe(n, e, found, now, budget):
            continue
        status, err = doctor.probe(n, cfg)
        health.note_probe(n, status == "ok", "probe", "" if status == "ok" else f"{status}: {err}"[-300:])
        if status == "ok":
            why[n] = "probe passed"
            continue
        if AUTH_ERR.search(err or ""):
            e["auth"] = f"auth error on probe ({(err.strip().splitlines() or [''])[-1][:80]}) → log in to {n}"
        else:
            e.pop("auth", None)
        _back_off(e, now)


def _canary_disabled(cfg, use, revs, found, now, budget, why):
    """A clean canary needs a parsed review, no marker and a clean sandbox; a marker or a write latches it."""
    eligible = limits.canary_eligible()
    for n in use:
        e = revs[n]
        if n not in eligible or not _may_probe(n, e, found, now, budget):
            continue
        trips = limits.trip_count(n)
        if trips is None:
            continue
        dirs = []

        def seed(repo, marker_dir):
            dirs.append(marker_dir)
            canary_seed(repo, marker_dir)
        try:
            status, err = doctor.probe(n, cfg, seed=seed)
            try:
                marks = sorted(m for d in dirs for m in os.listdir(d))
            except OSError:
                marks = None                         # can't tell: inconclusive
        finally:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
        if marks or status == "wrote-files":
            health.note_probe(n, False, "canary", f"wrote: {', '.join(marks or [status])}")
            limits.disable(n, "wrote in canary", run_id="canary", latch=True)
        elif status == "ok" and dirs and marks == []:
            health.note_probe(n, True, "canary")
            if limits.reenable(n, trips):
                why[n] = "canary clean"
        else:
            health.note_probe(n, False, "canary", f"inconclusive: {status}: {err}"[-300:])
            _back_off(e, now)


def canary_seed(repo, marker_dir):
    """Plant every write trap seen so far; each touches a file in `marker_dir` if it runs."""
    def touch(name):
        return f"touch {shlex.quote(os.path.join(marker_dir, name))}"
    files = {
        ".claude/settings.json": {"hooks": {ev: [{"hooks": [{"type": "command", "command": touch(f"claude-{ev}")}]}]
                                            for ev in ("SessionStart", "UserPromptSubmit", "Stop")}},
        ".github/hooks/tagteam-canary.json": {"version": 1, "hooks": {
            ev: [{"type": "command", "bash": touch(f"copilot-{ev}"), "timeoutSec": 10}]
            for ev in ("userPromptSubmitted", "postToolUse", "agentStop")}},
        ".mcp.json": {"mcpServers": {"tagteam-canary": {"command": "sh", "args": ["-c", touch("mcp")]}}},
    }
    for rel, data in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump(data, fh)


def _prune(cfg, now):
    """Run dirs older than 30 days, then stale worktrees of repos reviewed in the last 7, under the registry lock
    and threshold `sandbox.checkouts` uses. Lock files are never deleted. Returns how many run dirs went."""
    runs, gone = state.path("runs", "x").parent, 0
    for p in runs.iterdir():
        try:
            if p.is_dir() and not p.is_symlink() and now - p.stat().st_mtime > KEEP_RUNS_S:
                shutil.rmtree(p)
                gone += 1
        except OSError as e:
            state.log("run dir not pruned", path=str(p), error=repr(e))
    repos = set()
    for r in state.read_jsonl(state.path("runs.jsonl"), now - RECENT_S):
        req = state.read_json(runs / str(r.get("run_id")) / "request.json", {})
        repo = r.get("repo") or (req.get("repo") if isinstance(req, dict) else None)
        if isinstance(repo, str) and repo:
            repos.add(repo)
    stale = max(7200, 2 * cfg["review"]["deadline_s"])
    for repo in sorted(repos):
        try:
            with state.locked(sandbox._registry_lock(repo)):
                sandbox._clear_stale(repo, stale)
        except (OSError, RuntimeError) as e:
            state.log("stale worktrees not pruned", repo=repo, error=repr(e))
    return gone
