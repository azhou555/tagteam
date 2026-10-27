"""Live review state for the Claude status line, `tagteam watch`, `tagteam status` and the SwiftBar menu.
Imports only `state` up front (config only when a run is recorded) so a 2s tick stays cheap.

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
    """Running reviews, oldest first. A record whose owner died is recorded as an interrupted run by the one
    reader that claims it; one that passed `expires` is dropped."""
    now = now or time.time()
    out = []
    for p in sorted(_active_dir().glob("*.json")):
        rec = state.read_json(p)
        lock_path = p.with_suffix(".lock")
        alive = _alive(lock_path)
        if rec and now <= rec.get("expires", 0) and alive:
            out.append(rec)
            continue
        try:
            p.unlink()
        except FileNotFoundError:
            continue                                 # another reader claimed it, or the owner just finished
        except OSError:
            pass
        else:
            if rec and not alive:
                run = {**{k: rec[k] for k in ("run_id", "author", "session_id", "tag", "round", "max_rounds")},
                       "ts": rec["started"], "prior": rec.get("prior", 0),
                       "reviewers": list(rec["reviewers"]), "action": "allow", "blocking": 0, "advisory": 0,
                       "failed": [], "duration": round(now - rec["started"], 1), "reason": "interrupted"}
                _roll(run)
                _on_review(run)
        if not alive:                                # a live owner still holds it, and may rewrite its record
            with contextlib.suppress(OSError):
                lock_path.unlink()
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
    """Append a finished run, roll it into status.json, and hand it to the user's `hooks.on_review` command.
    A review killed earlier is recorded first, so it never lands after, and shows over, a later run."""
    active()
    _roll(run, now)
    _on_review(run)


def _on_review(run):
    """Start `hooks.on_review` detached with the run as JSON on stdin. Never waits on it, never raises."""
    import subprocess
    from . import config
    try:
        cmd = config.load()["hooks"]["on_review"]
        if not cmd:
            return
        kind = outcome(run)[0]
        env = {**os.environ, "TAGTEAM_OUTCOME": kind, "TAGTEAM_SUMMARY": summary(run), "TAGTEAM_DEPTH": "1"}
        p = subprocess.Popen(["sh", "-c", cmd], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, env=env)
        with contextlib.suppress(BrokenPipeError), p.stdin:     # a command may exit without reading it
            p.stdin.write(json.dumps({**run, "outcome": kind, "summary": summary(run)}).encode())
    except Exception as e:                           # a notification must never cost the hook its verdict
        state.log("on_review command failed", error=repr(e))


def _roll(run, now=None):
    """Append and roll up, atomically with respect to other writers. The rollup counts the run on the day it
    finished (`now`); backfill passes the run's own start time."""
    now = now or time.time()
    with state.locked("runs"):
        state.append_jsonl(state.path("runs.jsonl"), run)
        s = _load_status(now)
        s["today"] += bool(run.get("reviewers"))      # a notice or a revert is not a review
        s["sessions"] = {k: v for k, v in s["sessions"].items() if v["ts"] >= now - SESSION_TTL}
        s["sessions"][f"{run['author']}:{run['session_id']}"] = {
            k: run[k] for k in ("ts", "action", "blocking", "advisory", "reviewers", "failed", "round")}
        s["sessions"][f"{run['author']}:{run['session_id']}"].update(
            {k: run[k] for k in ("prior", "confirm", "reason", "max_rounds") if k in run})
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
        _roll(run, now=run["ts"])                    # history: no on_review
    return len(runs)


def note_error(now=None):
    now = now or time.time()
    with state.locked("runs"):
        s = _load_status(now)
        s["errors"].append(now)
        state.write_json(state.path("status.json"), s)


# Pastel palette. ansi: 256-color code for terminals. hex: SwiftBar "light-mode,dark-mode" (deeper shade on a
# light menu bar, where pastels wash out).
BRAND = {"ansi": 108, "hex": "#5E8C61,#A3C9A8", "sf": "person.2.fill"}            # sage
KINDS = {
    "running": {"icon": "⟳", "label": "Reviewing", "ansi": 223, "sf": "arrow.triangle.2.circlepath",
                "hex": "#C27C2C,#FFD8A8"},
    "passed": {"icon": "✓", "label": "Review passed", "ansi": 158, "sf": "checkmark.seal.fill",
               "hex": "#3A9E6F,#B5EAD7"},
    "blocked": {"icon": "✗", "label": "Changes requested", "ansi": 217, "sf": "xmark.octagon.fill",
                "hex": "#C8554F,#FFB3B3"},
    "resolved": {"icon": "✓", "label": "Issues resolved", "ansi": 158, "sf": "checkmark.seal.fill",
                 "hex": "#3A9E6F,#B5EAD7"},
    "unresolved": {"icon": "⚠", "label": "Unresolved", "ansi": 216, "sf": "exclamationmark.triangle.fill",
                   "hex": "#C46A3A,#FFC9A8"},
    "unreviewed": {"icon": "⚠", "label": "Not reviewed", "ansi": 229, "sf": "exclamationmark.circle.fill",
                   "hex": "#B08A1E,#FFF1A8"},
    "failed": {"icon": "⚠", "label": "Review failed", "ansi": 229, "sf": "exclamationmark.circle.fill",
               "hex": "#B08A1E,#FFF1A8"},
    "interrupted": {"icon": "⚠", "label": "Review interrupted", "ansi": 229, "sf": "exclamationmark.circle.fill",
                    "hex": "#B08A1E,#FFF1A8"},
}


REASONS = {"no reviewers configured": "no reviewers configured; run tagteam install"}


def outcome(run):
    """(kind, detail) for a finished run, in plain words. `prior` is how many issues were open going in."""
    prior, m = run.get("prior") or 0, run.get("max_rounds")
    reason = run.get("reason")
    if reason == "reverted":
        return "resolved", "changes reverted"
    owed = f" · {_plural(prior, 'earlier issue')} not re-checked" if prior else ""
    if reason == "interrupted":
        return "interrupted", "stopped before reviewers finished" + owed
    if reason:
        return "unreviewed", REASONS.get(reason, reason) + owed
    n = run["blocking"]
    if run.get("confirm") and n:                     # may be a re-issued block with no reviewer spawned
        return "unresolved", f"{_plural(n, 'issue')} still open after {_plural(m, 'fix round')}"
    if not run["reviewers"] and run["action"] != "block":      # a re-issued block spawned no one, but it is a verdict
        return "unreviewed", "no reviewer had quota available" + owed
    if run["reviewers"] and set(run["failed"]) >= set(run["reviewers"]):
        return "failed", "every reviewer errored or timed out" + owed
    where = f" (round {run['round']}/{m})" if m else ""
    if run["action"] == "block":
        return "blocked", (f"{_plural(n, 'issue')} to fix{where}" if not prior else
                           f"{_plural(n, 'issue')} still open after fixes{where}")
    notes = f", {_plural(run['advisory'], 'minor note')}" if run["advisory"] else ""
    if prior:
        return "resolved", f"all {prior} fixed and re-checked{notes}"
    if run["advisory"]:
        return "passed", f"no blocking issues{notes}"
    return "passed", "no issues found"


def summary(run):
    """Plain one-line outcome for logs, `status`, `watch`, and the completion message."""
    kind, detail = outcome(run)
    return f"{KINDS[kind]['icon']} {KINDS[kind]['label']}: {detail}"


def _dur(secs):
    secs = int(secs or 0)
    return f"{secs // 60}m{secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def _ago(ts, now):
    secs = max(0, int(now - ts))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago" if secs < 86400 else f"{secs // 86400}d ago"


class _Paint:
    """ANSI helpers that collapse to plain text when color is off."""

    def __init__(self, on):
        self.on = on

    def fg(self, code, text):
        return f"\x1b[38;5;{code}m{text}\x1b[0m" if self.on else text

    def dim(self, text):
        return f"\x1b[2m{text}\x1b[0m" if self.on else text

    def kind(self, kind, text):
        return self.fg(KINDS[kind]["ansi"], text)

    def brand(self, text):
        return self.fg(BRAND["ansi"], text)


def _progress(rec, now, paint=_Paint(False)):
    parts = []
    for r, st in rec["reviewers"].items():
        if st["state"] == "running":
            parts.append(f"{r} {paint.kind('running', '…')} {_dur(now - rec['started'])}")
        else:
            ok = st["state"] == "done"
            parts.append(f"{r} {paint.kind('passed' if ok else 'blocked', '✓' if ok else '✗')} {_dur(st.get('secs'))}")
    return " · ".join(parts)


def _plural(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


def segment(agent, session_id, now=None, color=False):
    """The tagteam line for one agent session's status bar."""
    now = now or time.time()
    paint = _Paint(color)
    running = active(now)
    mine = [r for r in running if r["author"] == agent and r["session_id"] == session_id]
    s = _load_status(now)
    head = paint.brand("tagteam")
    if mine:
        r = mine[-1]
        what = "plan" if r["tag"] == "plan-gate" else "changes"
        parts = [f"{head} {paint.kind('running', f'⟳ Reviewing {what}')}, round {r['round']}/{r['max_rounds']}",
                 _progress(r, now, paint)]
    elif last := s["sessions"].get(f"{agent}:{session_id}"):
        kind, detail = outcome(last)
        parts = [f"{head} {paint.kind(kind, summary(last))}", paint.dim(_ago(last["ts"], now))]
    else:
        parts = [f"{head} {paint.dim('no review yet this session')}"]
    parts.append(paint.dim(f"{_plural(s['today'], 'review')} today"))
    if len(running) > len(mine):
        parts.append(paint.kind("running", f"+{len(running) - len(mine)} running elsewhere"))
    if s["errors"]:
        parts.append(paint.kind("blocked", f"⚠ {_plural(len(s['errors']), 'hook error')} (24h)"))
    return " · ".join(parts)


def _today_and_week(now):
    week = state.read_jsonl(state.path("runs.jsonl"), now - 7 * 86400)
    return [r for r in week if r.get("reviewers") and _day(r["ts"] + r["duration"]) == _day(now)], week   # by finish day


def _num(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _usages(run):
    u = run.get("usage")
    return [x for x in u.values() if isinstance(x, dict)] if isinstance(u, dict) else []


def _week(now):
    """Last 7 days of runs in which a reviewer was actually asked; others spent nothing."""
    return [r for r in state.read_jsonl(state.path("runs.jsonl"), now - 7 * 86400) if r.get("reviewers")]


def week_spend(now=None, week=None):
    """(reviews, cost_usd, unreported) over the last 7 days. A review reported cost if any reviewer gave cost_usd."""
    week = _week(now or time.time()) if week is None else week
    costs = [[c for u in _usages(r) if (c := _num(u, "cost_usd")) is not None] for r in week]
    return len(week), sum(sum(c) for c in costs), sum(not c for c in costs)


def spend_line(reviews, cost, unreported, premium):
    line = f"This week: {_plural(reviews, 'review')} · "
    if unreported == reviews:
        line += "cost not reported"
    else:
        line += f"~${cost:.2f} reported" + (f" · {unreported} didn't report cost" if unreported else "")
    return line + (f" · {_plural(premium, 'premium request')}" if premium else "")


def week_lines(now=None):
    """The weekly spend line and, under it, reviews per reviewer."""
    week = _week(now or time.time())
    premium = sum(n for r in week for u in _usages(r) if (n := _num(u, "premium_requests")) is not None)
    return [spend_line(*week_spend(week=week), premium)] + (
        [f"by reviewer: {_counts(week, 'reviewers')}"] if any(r["reviewers"] for r in week) else [])


def _counts(runs, key):
    counts = {}
    for r in runs:
        for k in (r[key] if isinstance(r[key], list) else [r[key]]):
            counts[k] = counts.get(k, 0) + 1
    return "  ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))


def _when(ts, now):
    return time.strftime("%H:%M" if _day(ts) == _day(now) else "%a %H:%M", time.localtime(ts))


def render_watch(now=None, recent=5, color=False):
    now = now or time.time()
    paint = _Paint(color)
    s = _load_status(now)
    lines = [f"tagteam watch  {time.strftime('%H:%M:%S', time.localtime(now))}", ""]
    running = active(now)
    lines.append(f"Running ({len(running)}):")
    lines += [f"  {paint.kind('running', '⟳')} {r['author']:7} {r['tag']} round {r['round']}/{r['max_rounds']}  "
              f"{_progress(r, now, paint)}" for r in running] or ["  none"]
    today, week = _today_and_week(now)
    lines.append(f"\nReviews today: {len(today)}")
    for key, label in (("author", "by author"), ("reviewers", "by reviewer")):
        if today:
            lines.append(f"  {label:12} {_counts(today, key)}")
    lines.append("\n" + "\n".join(week_lines(now)))
    lines.append(f"\nLast {recent}:")
    for r in week[-recent:][::-1]:
        kind = outcome(r)[0]
        lines.append(f"  {_when(r['ts'], now):9}  {r['author']:7} -> {','.join(r['reviewers']) or '-':14} "
                     f"{paint.kind(kind, summary(r))}  ({_dur(r['duration'])})")
    if not week:
        lines.append("  none")
    if s["errors"]:
        lines.append("\n" + paint.kind("blocked", f"⚠ {_plural(len(s['errors']), 'hook error')} in 24h: "
                                                  f"see {state.path('tagteam.log')}"))
    return "\n".join(lines)


def watch(once=False):
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    while True:
        out = render_watch(color=color)
        if once:
            print(out)
            return
        sys.stdout.write("\x1b[H\x1b[2J" + out + "\n")
        sys.stdout.flush()
        time.sleep(1)


def _sb(text, **params):
    """One SwiftBar line; `|` in text would start the params, so swap it out."""
    text = text.replace("|", "¦")
    return text + (" | " + " ".join(f"{k}={v}" for k, v in params.items()) if params else "")


def render_swiftbar(now=None, recent=8):
    """SwiftBar plugin output: a menu bar icon + today's count, and a dropdown that mirrors `watch`."""
    now = now or time.time()
    s = _load_status(now)
    running = active(now)
    today, week = _today_and_week(now)
    last = week[-1] if week else None
    if running:
        kind, title = "running", _dur(now - min(r["started"] for r in running))
    else:
        kind, title = (outcome(last)[0], str(len(today))) if last else (None, "0")
    head = {"sfcolor1": BRAND["hex"]}
    if kind:
        head["sfcolor2"] = KINDS[kind]["hex"]
    icon = f":{BRAND['sf']}: :{KINDS[kind]['sf'] if kind else 'checkmark.seal'}:"
    if s["errors"]:
        title += " :exclamationmark.triangle.fill:"
        head["sfcolor3"] = KINDS["blocked"]["hex"]
    tip = "tagteam: " + (f"{len(running)} review(s) running" if running else
                         summary(last) if last else "no reviews yet")
    lines = [_sb(f"{icon} {title}", tooltip=f'"{tip}"', **head), "---"]
    if running:
        lines.append(_sb("Running", size=11, color="gray"))
        for r in running:
            lines.append(_sb(f"⟳ {r['author']} → {', '.join(r['reviewers'])} · round {r['round']}/{r['max_rounds']}",
                             color=KINDS["running"]["hex"]))
            lines.append(_sb(f"    {_progress(r, now)}", font="Menlo", size=12))
        lines.append("---")
    lines.append(_sb(f"Reviews today: {len(today)}", size=11, color="gray"))
    if today:
        lines.append(_sb(f"by author:  {_counts(today, 'author')}", font="Menlo", size=12))
        lines.append(_sb(f"by reviewer:  {_counts(today, 'reviewers')}", font="Menlo", size=12))
    lines += [_sb(week_lines(now)[0], size=11, color="gray"),
              *[_sb(t, font="Menlo", size=12) for t in week_lines(now)[1:]],
              "---", _sb("Recent reviews (click to open the run)", size=11, color="gray")]
    for r in week[-recent:][::-1]:
        kind_r = outcome(r)[0]
        run_dir = state.path("runs", r["run_id"], "x").parent
        lines.append(_sb(f"{_when(r['ts'], now):9}  {r['author']} → {', '.join(r['reviewers']) or '—'}: {summary(r)}",
                         color=KINDS[kind_r]["hex"], bash="/usr/bin/open", param1=f'"{run_dir}"', terminal="false"))
    if not week:
        lines.append(_sb("none yet", color="gray"))
    lines.append("---")
    if s["errors"]:
        lines.append(_sb(f"⚠ {_plural(len(s['errors']), 'hook error')} in 24h: reviews were skipped. Open log",
                         color=KINDS["blocked"]["hex"], bash="/usr/bin/open", param1="-t",
                         param2=f'"{state.path("tagteam.log")}"', terminal="false"))
    from . import install
    exe, *args = install.entry_argv()
    params = {f"param{i}": f'"{a}"' for i, a in enumerate([*args, "watch"], 1)}
    lines.append(_sb("Open live view (tagteam watch)", bash=f'"{exe}"', **params, terminal="true"))
    lines.append(_sb("Refresh", refresh="true"))
    return "\n".join(lines)


def statusline_segment(raw):
    data = json.loads(raw or "{}")
    return segment("claude", data.get("session_id", ""), color=not os.environ.get("NO_COLOR"))


def main(argv):
    """`swiftbar` renders the menu bar item; otherwise print the status segment for Claude's status JSON on stdin.
    Never fails loudly."""
    try:
        if argv[:1] == ["swiftbar"]:
            print(render_swiftbar())
            return
        print(statusline_segment(sys.stdin.read()))
    except Exception:
        if argv[:1] == ["swiftbar"]:
            print("tagteam ⚠\n---\nfailed to render; see ~/.tagteam/state/tagteam.log")


if __name__ == "__main__":
    main(sys.argv[1:])
