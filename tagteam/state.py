"""Paths, config loading, locked JSON state, logging."""
import contextlib
import fcntl
import json
import os
import pathlib
import time
import tomllib

from . import resources

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOME = pathlib.Path(os.environ.get("TAGTEAM_HOME", pathlib.Path.home() / ".tagteam"))
STATE = HOME / "state"


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(repo=None):
    cfg = tomllib.loads(resources.text("config.default.toml"))
    for p in [HOME / "config.toml", pathlib.Path(repo) / ".tagteam.toml" if repo else None]:
        if p and p.is_file():
            _merge(cfg, tomllib.loads(p.read_text()))
    return cfg


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
