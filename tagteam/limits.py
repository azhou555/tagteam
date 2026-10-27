"""Hard loop stops: hourly reservation, per-session digest memory, per-session lock, failure breaker."""
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import time

from . import quota, state

HOUR = 3600
STATE_ERROR = "state error, see tagteam.log"
MAX_DIGESTS = 50
BREAKER_FAILURES = 2


def safe_session_key(agent, session_id):
    """`<agent>-<session id>` made safe for a file name: names the session's state file and lock."""
    return f"{agent}-" + "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id or "nosession")


def limit_reason(cfg):
    return f"review limit reached ({cfg['limits']['max_reviews_per_hour']} per hour)"


def reserve(run_id, session_key, now, cfg):
    """Take one of the hour's review slots before any reviewer starts. None if taken, else why not to review."""
    try:
        with state.locked("limits"):
            p = state.path("reservations.jsonl")
            live = state.read_jsonl(p, now - HOUR)
            if len(live) >= cfg["limits"]["max_reviews_per_hour"]:
                state.log("hourly review limit reached", run_id=run_id, session=session_key, count=len(live))
                return limit_reason(cfg)
            live.append({"run_id": run_id, "session": session_key, "ts": now})
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in live))
            os.replace(tmp, p)
            return None
    except OSError as e:
        state.log("reservation state unusable, not reviewing", run_id=run_id, error=repr(e))
        return STATE_ERROR


def digest(artifacts, extra=""):
    """Hash of the diffs reviewed; `extra` (an author's rebuttal) makes a re-review of the same diff its own entry."""
    h = hashlib.sha256()
    if extra:
        h.update(f"{len(extra)}:{extra}".encode())      # length-prefixed so it can't run into the diff parts
    for part in sorted(a["path"] + "\0" + a["diff"] for a in artifacts):
        h.update(part.encode() + b"\1")
    return h.hexdigest()


def _session_path(session_key):
    return state.path("sessions", f"{session_key}.json")


def remembered(session_key, dig):
    """The verdict {action, blocking, message} a finished run recorded for this digest this turn, if any."""
    return (state.read_json(_session_path(session_key), {}).get("digests") or {}).get(dig)


def remember(session_key, dig, action, blocking, message=None, turn_id=None):
    """Best effort: a finished verdict is never lost to a state error. Memory is per turn, so nothing is
    written once a new turn (another `turn_id`) owns the session file."""
    try:
        with state.locked(session_key):
            p = _session_path(session_key)
            s = state.read_json(p)
            if not s or s.get("turn_id") != turn_id:
                return
            d = s.setdefault("digests", {})
            d.pop(dig, None)
            d[dig] = {"action": action, "blocking": blocking, "message": message}
            while len(d) > MAX_DIGESTS:
                del d[next(iter(d))]
            state.write_json(p, s)
    except OSError as e:
        state.log("digest not remembered", session=session_key, error=repr(e))


def note_result(reviewer, ok, cfg):
    """Two failures in a row pause the reviewer for cooldown_min; any success clears the count. Best effort."""
    try:
        with state.locked("failures"):
            p = state.path("failures.json")
            counts = state.read_json(p, {})
            tripped = not ok and counts.get(reviewer, 0) + 1 >= BREAKER_FAILURES
            n = 0 if ok or tripped else counts.get(reviewer, 0) + 1
            if n != counts.get(reviewer, 0):
                counts[reviewer] = n
                state.write_json(p, counts)
        if tripped:
            state.log("reviewer failed twice in a row; paused", reviewer=reviewer,
                      minutes=cfg["limits"]["cooldown_min"])
            quota.cooldown(reviewer, cfg["limits"]["cooldown_min"])
    except OSError as e:
        state.log("failure count not updated", reviewer=reviewer, error=repr(e))


HELD, BUSY, ERROR = "held", "busy", "error"


@contextlib.contextmanager
def session_lock(author, session_id):
    """Yields HELD, BUSY (another review has this session) or ERROR (the lock itself failed).
    The kernel frees the lock if the holder dies."""
    try:
        f = open(state.path("locks", f"session-{safe_session_key(author, session_id)}.lock"), "w")
    except OSError as e:
        state.log("session lock unavailable", error=repr(e))
        yield ERROR
        return
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                state.log("session lock failed", error=repr(e))
            yield BUSY if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else ERROR
            return
        yield HELD


def disabled():
    """{reviewer: {reason, ts}} for reviewers that wrote to the real tree, until `clear_disabled`.
    None if the list exists but can't be read: every reviewer counts as disabled until `doctor --clear`."""
    return state.read_dict(state.path("disabled.json"))


def disable(reviewer, reason):
    """Best effort: a failed write is logged, never raised."""
    try:
        with state.locked("disabled"):
            d = disabled()
            if d is None:                            # rewriting it would re-enable everyone else
                state.log("disabled list unreadable; left as is", reviewer=reviewer, reason=reason)
                return
            d[reviewer] = {"reason": reason, "ts": time.time()}
            state.write_json(state.path("disabled.json"), d)
    except OSError as e:
        state.log("reviewer not disabled", reviewer=reviewer, error=repr(e))


def clear_disabled():
    try:
        with state.locked("disabled"):
            state.write_json(state.path("disabled.json"), {})
    except OSError as e:
        state.log("disabled reviewers not cleared", error=repr(e))


def clear_pauses():
    """Forget cooldowns and failure counts (`doctor --clear`). False if the state couldn't be written."""
    try:
        for name in ("cooldowns", "failures"):
            with state.locked(name):
                state.write_json(state.path(f"{name}.json"), {})
        return True
    except OSError as e:
        state.log("paused reviewers not cleared", error=repr(e))
        return False
