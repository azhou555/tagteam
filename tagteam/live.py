"""Live review state for the status line and `tagteam watch`. Imports only `state` so a 2s tick stays cheap.

active/<run_id>.json   one per running review; its owner holds active/<run_id>.lock for its whole life
runs.jsonl             one line per finished review (a "review" = a run, not a reviewer call)
status.json            rollup the status line reads: today's count, last result per session, recent hook errors
"""
import contextlib
import fcntl
import json
import os
import sys
import time

from . import state

ERROR_WINDOW = 86400
SESSION_TTL = 7 * 86400


def _active_dir():
    return state.path("active", "x").parent


@contextlib.contextmanager
def tracking(rec):
    """Publish `rec` as a running review until the block exits; yields update(reviewer, meta)."""
    d = _active_dir()
    lock_path, json_path = d / f"{rec['run_id']}.lock", d / f"{rec['run_id']}.json"
    rec = {**rec, "pid": os.getpid(), "reviewers": {r: {"state": "running"} for r in rec["reviewers"]}}
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state.write_json(json_path, rec)

        def update(reviewer, meta):
            rec["reviewers"][reviewer] = {"state": "done" if meta.get("ok") else "failed",
                                          "secs": meta.get("duration")}
            state.write_json(json_path, rec)
        try:
            yield update
        finally:
            for p in (json_path, lock_path):
                with contextlib.suppress(OSError):
                    p.unlink()


def _alive(lock_path):
    try:
        with open(lock_path, "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def active(now=None):
    """Running reviews, oldest first. Records whose owner died (or that passed `expires`) are dropped."""
    now = now or time.time()
    out = []
    for p in sorted(_active_dir().glob("*.json")):
        rec = state.read_json(p)
        lock_path = p.with_suffix(".lock")
        if rec and now <= rec.get("expires", 0) and _alive(lock_path):
            out.append(rec)
            continue
        for q in (p, lock_path):
            with contextlib.suppress(OSError):
                q.unlink()
    return out


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _load_status(now):
    s = state.read_json(state.path("status.json")) or {}
    if s.get("day") != _day(now):
        s["day"], s["today"] = _day(now), 0
    s.setdefault("sessions", {})
    s["errors"] = [t for t in s.get("errors", []) if t >= now - ERROR_WINDOW]
    return s


def record_run(run, now=None):
    """Append a finished run and roll it into status.json, atomically with respect to other writers.

    The rollup counts the run on the day it finished (`now`); backfill passes the run's own start time.
    """
    now = now or time.time()
    with state.locked("runs"):
        state.append_jsonl(state.path("runs.jsonl"), run)
        s = _load_status(now)
        s["today"] += 1
        s["sessions"] = {k: v for k, v in s["sessions"].items() if v["ts"] >= now - SESSION_TTL}
        s["sessions"][f"{run['author']}:{run['session_id']}"] = {
            k: run[k] for k in ("ts", "action", "blocking", "advisory", "reviewers", "failed", "round")}
        state.write_json(state.path("status.json"), s)


def backfill():
    """Seed runs.jsonl from finished run dirs once, so counts include reviews from before it existed."""
    if state.path("runs.jsonl").exists():
        return 0
    calls = {}
    for h in state.read_jsonl(state.path("history.jsonl")):
        calls.setdefault(h["run_id"], []).append(h.get("duration", 0))
    runs = []
    for v in sorted(state.path("runs", "x").parent.glob("*/verdict.json")):
        req, d = state.read_json(v.parent / "request.json", {}), state.read_json(v, {})
        try:
            ts = time.mktime(time.strptime(v.parent.name.split("-")[0], "%Y%m%dT%H%M%S"))
        except ValueError:
            continue
        runs.append({"ts": ts, "run_id": v.parent.name, "author": req.get("author", "?"),
                     "session_id": req.get("session_id", ""), "tag": req.get("tag", "turn"),
                     "round": req.get("round", 1), "reviewers": list(req.get("assignments", {})),
                     "action": d.get("action", "allow"), "blocking": len(d.get("blocking", [])),
                     "advisory": len(d.get("advisory", [])), "failed": d.get("failed", []),
                     "duration": max(calls.get(v.parent.name, [0]))})
    for run in runs:
        record_run(run, now=run["ts"])
    return len(runs)


def note_error(now=None):
    now = now or time.time()
    with state.locked("runs"):
        s = _load_status(now)
        s["errors"].append(now)
        state.write_json(state.path("status.json"), s)


def summary(run):
    """One-line outcome, shared by the completion message and the status line."""
    if not run["reviewers"]:
        return "⚠ no reviewer available"
    if set(run["failed"]) >= set(run["reviewers"]):
        return "⚠ all reviewers failed"
    if run["action"] == "block":
        return f"✗ {run['blocking']} blocking"
    minor = f" ({run['advisory']} minor)" if run["advisory"] else ""
    return f"✓ clean{minor}"


def _dur(secs):
    secs = int(secs or 0)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def _progress(rec, now):
    parts = []
    for r, st in rec["reviewers"].items():
        if st["state"] == "running":
            parts.append(f"{r} … {_dur(now - rec['started'])}")
        else:
            parts.append(f"{r} {'✓' if st['state'] == 'done' else '✗'} {_dur(st.get('secs'))}")
    return " · ".join(parts)


def segment(agent, session_id, now=None):
    """The tagteam line for one agent session's status bar."""
    now = now or time.time()
    running = active(now)
    mine = [r for r in running if r["author"] == agent and r["session_id"] == session_id]
    s = _load_status(now)
    if mine:
        r = mine[-1]
        what = "plan" if r["tag"] == "plan-gate" else "changes"
        parts = [f"tagteam ⟳ reviewing {what} {r['round']}/{r['max_rounds']}", _progress(r, now)]
    else:
        last = s["sessions"].get(f"{agent}:{session_id}")
        parts = [f"tagteam {summary(last)}" if last else "tagteam"]
    parts.append(f"{s['today']} review{'s' if s['today'] != 1 else ''} today")
    if len(running) > len(mine):
        parts.append(f"+{len(running) - len(mine)} running elsewhere")
    if s["errors"]:
        parts.append(f"⚠ {len(s['errors'])} hook error{'s' if len(s['errors']) != 1 else ''} (24h)")
    return " · ".join(parts)


def render_watch(now=None, recent=5):
    now = now or time.time()
    s = _load_status(now)
    lines = [f"tagteam watch  {time.strftime('%H:%M:%S', time.localtime(now))}", ""]
    running = active(now)
    lines.append(f"Running ({len(running)}):")
    lines += [f"  {r['author']:7} {r['tag']} round {r['round']}/{r['max_rounds']}  {_progress(r, now)}"
              for r in running] or ["  none"]
    week = state.read_jsonl(state.path("runs.jsonl"), now - 7 * 86400)
    today = [r for r in week if _day(r["ts"]) == _day(now)]
    lines.append(f"\nReviews today: {len(today)}")
    for key, label in (("author", "by author"), ("reviewers", "by reviewer")):
        counts = {}
        for r in today:
            for k in (r[key] if isinstance(r[key], list) else [r[key]]):
                counts[k] = counts.get(k, 0) + 1
        if counts:
            lines.append(f"  {label:12} " + "  ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1])))
    lines.append(f"\nLast {recent}:")
    for r in week[-recent:][::-1]:
        when = time.strftime("%H:%M" if _day(r["ts"]) == _day(now) else "%a %H:%M", time.localtime(r["ts"]))
        lines.append(f"  {when:9}  {r['author']:7} -> "
                     f"{','.join(r['reviewers']) or '-':14} {summary(r)}  ({_dur(r['duration'])})")
    if not week:
        lines.append("  none")
    if s["errors"]:
        lines.append(f"\n⚠ {len(s['errors'])} hook error(s) in 24h: see {state.path('tagteam.log')}")
    return "\n".join(lines)


def watch(once=False):
    while True:
        out = render_watch()
        if once:
            print(out)
            return
        sys.stdout.write("\x1b[H\x1b[2J" + out + "\n")
        sys.stdout.flush()
        time.sleep(1)


def statusline_main():
    """Status-line entry: read Claude's status JSON on stdin, print the tagteam line. Never fails loudly."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
        print(segment("claude", data.get("session_id", "")))
    except Exception:
        pass


if __name__ == "__main__":
    statusline_main()
