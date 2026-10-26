"""Hard loop stops: hourly reservation, per-session digest memory, per-session lock, failure breaker."""
import contextlib
import fcntl
import hashlib
import json
import os
import time

from . import quota, state

HOUR = 3600
MAX_DIGESTS = 50
BREAKER_FAILURES = 2


def limit_reason(cfg):
    return f"review limit reached ({cfg['limits']['max_reviews_per_hour']} per hour)"


def reserve(run_id, session_key, now, cfg):
    """Take one of the hour's review slots before any reviewer starts. False means do not review."""
    try:
        with state.locked("limits"):
            p = state.path("reservations.jsonl")
            live = state.read_jsonl(p, now - HOUR)
            if len(live) >= cfg["limits"]["max_reviews_per_hour"]:
                state.log("hourly review limit reached", run_id=run_id, session=session_key, count=len(live))
                return False
            live.append({"run_id": run_id, "session": session_key, "ts": now})
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in live))
            os.replace(tmp, p)
            return True
    except OSError as e:
        state.log("reservation state unusable, not reviewing", run_id=run_id, error=repr(e))
        return False


def digest(artifacts):
    h = hashlib.sha256()
    for part in sorted(a["path"] + "\0" + a["diff"] for a in artifacts):
        h.update(part.encode() + b"\1")
    return h.hexdigest()


def _session_path(session_key):
    return state.path("sessions", f"{session_key}.json")


def remembered(session_key, dig):
    """The verdict {action, blocking, message} a finished run recorded for this digest this turn, if any."""
    return (state.read_json(_session_path(session_key), {}).get("digests") or {}).get(dig)


def remember(session_key, dig, action, blocking, message=None):
    with state.locked(session_key):
        p = _session_path(session_key)
        s = state.read_json(p)
        if not s:
            return
        d = s.setdefault("digests", {})
        d.pop(dig, None)
        d[dig] = {"action": action, "blocking": blocking, "message": message}
        while len(d) > MAX_DIGESTS:
            del d[next(iter(d))]
        state.write_json(p, s)


def note_result(reviewer, ok, cfg):
    """Two failures in a row pause the reviewer for cooldown_min; any success clears the count."""
    with state.locked("failures"):
        p = state.path("failures.json")
        counts = state.read_json(p, {})
        tripped = not ok and counts.get(reviewer, 0) + 1 >= BREAKER_FAILURES
        n = 0 if ok or tripped else counts.get(reviewer, 0) + 1
        if n != counts.get(reviewer, 0):
            counts[reviewer] = n
            state.write_json(p, counts)
    if tripped:
        state.log("reviewer failed twice in a row; paused", reviewer=reviewer, minutes=cfg["limits"]["cooldown_min"])
        quota.cooldown(reviewer, cfg["limits"]["cooldown_min"])


@contextlib.contextmanager
def session_lock(author, session_id):
    """Yields True if no other review holds this session, else False. The kernel frees it if the holder dies."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id or "nosession")
    try:
        f = open(state.path("locks", f"session-{author}-{safe}.lock"), "w")
    except OSError as e:
        state.log("session lock unavailable, not reviewing", error=repr(e))
        yield False
        return
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
