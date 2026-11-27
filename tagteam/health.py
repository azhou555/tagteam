"""Advisory reviewer health: recent call outcomes (history plus probes) demote a failing reviewer, never block it."""
import time

from . import state

WINDOW = 7 * 86400


def calls(reviewer, n=4):
    """The last `n` call outcomes for `reviewer`, oldest first."""
    since = time.time() - WINDOW
    rows = [r for f in ("history.jsonl", "probes.jsonl") for r in state.read_jsonl(state.path(f), since)
            if r.get("reviewer") == reviewer]
    rows.sort(key=lambda r: r.get("ts", 0))
    return [bool(r.get("ok")) for r in rows[-n:]]


def last_real_call_ok(reviewer):
    """Whether `reviewer`'s latest real call (history.jsonl, not a probe) in the window succeeded."""
    rows = [r for r in state.read_jsonl(state.path("history.jsonl"), time.time() - WINDOW)
            if r.get("reviewer") == reviewer]
    return bool(rows) and bool(max(rows, key=lambda r: r.get("ts", 0)).get("ok"))


def is_demoted(reviewer):
    c = calls(reviewer)
    return len(c) >= 4 and c.count(False) >= 3 and not c[-1]


def note_probe(reviewer, ok, kind, detail=""):
    """kind: "probe" or "canary"."""
    state.append_jsonl(state.path("probes.jsonl"), {"ts": time.time(), "reviewer": reviewer, "ok": ok, "probe": True,
                                                     "kind": kind, "detail": detail})


def load():
    d = state.read_json(state.path("health.json"), {})
    return d if isinstance(d, dict) else {}


def save(d):
    with state.locked("health"):
        state.write_json(state.path("health.json"), d)


def update(fn):
    """Write fn(current dict) unless it returns None, all under the health lock; returns what fn returned.
    Lock order: heal / heal-run, then health; never take those while holding this one."""
    with state.locked("health"):
        d = fn(load())
        if d is not None:
            state.write_json(state.path("health.json"), d)
        return d


def event(reviewer, action, why):
    state.append_jsonl(state.path("heal.jsonl"),
                       {"ts": time.time(), "reviewer": reviewer, "action": action, "why": why})


def events(n=5):
    return state.read_jsonl(state.path("heal.jsonl"))[-n:]


def needs_you(d=None):
    """[(key, text)] for what only the user can fix; reviewers keyed by name, authors as "author:<name>"."""
    d = load() if d is None else d
    out = []
    for section, prefix in (("reviewers", ""), ("authors", "author:")):
        for name, e in (d.get(section) if isinstance(d.get(section), dict) else {}).items():
            if isinstance(e, dict) and e.get("status") == "needs_you":
                out.append((prefix + name, f"{name}: {e.get('reason') or 'needs you'}"))
    return out


def notice(now=None):
    """" · ⚠ text; text" for needs-you items not shown in the last day (stamped as shown), else ""."""
    now = time.time() if now is None else now
    fresh = []

    def stamp(d):
        seen = d.get("noticed") if isinstance(d.get("noticed"), dict) else {}
        fresh.extend((k, t) for k, t in needs_you(d) if now - seen.get(k, 0) >= 86400 or seen.get(k, 0) > now)
        return {**d, "noticed": {**seen, **{k: now for k, _ in fresh}}} if fresh else None
    update(stamp)
    return " · ⚠ " + "; ".join(t for _, t in fresh) if fresh else ""
