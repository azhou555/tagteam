"""Paths, locked JSON state, logging."""
import contextlib
import fcntl
import json
import os
import pathlib
import time

HOME = pathlib.Path(os.environ.get("TAGTEAM_HOME", pathlib.Path.home() / ".tagteam"))
STATE = HOME / "state"


def extras_off():
    """TAGTEAM_NO_MACOS_EXTRAS=1 (clean-install runs): install and uninstall leave SwiftBar, the launcher and the status line alone."""
    return os.environ.get("TAGTEAM_NO_MACOS_EXTRAS", "").lower() in ("1", "true", "yes")


def path(*parts):
    p = STATE.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def locked(name="global"):
    with open(path("locks", name + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def read_json(p, default=None):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except (OSError, ValueError):
        return default


def read_dict(p):
    """{} if `p` doesn't exist, its JSON object if it parses, else None. Safety lists read None as "everyone"."""
    p = pathlib.Path(p)
    if not p.exists():
        return {}
    d = read_json(p)
    return d if isinstance(d, dict) else None


def write_json(p, data):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def append_jsonl(p, rec):
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")


def read_jsonl(p, since=0.0):
    out = []
    try:
        with open(p) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ts", 0) >= since:
                    out.append(r)
    except OSError:
        pass
    return out


def log(msg, **kw):
    append_jsonl(path("tagteam.log"), {"ts": time.time(), "msg": msg, **kw})
