"""Shadow ownership ledger (Claude): which tool call changed which file, logged against today's whole-tree diff.

Nothing here may change what a hook returns; core calls every entry point through a guard that logs and
swallows. See docs/attribution-redesign.md.

Session ledger  ledger/claude-<sid>.json  {"pending": {call: pending}, "calls": [closed], "tracked": {repo: n}}
  pending  {call, agent_id, tool, t0, repo, kind: files|tree|skipped, pre: {abs path: blob|None} | tree id, snapshot_ms?}
  closed   {call, agent_id, tool, kind, t0, t1, repo, deltas: {abs path: [pre blob, post blob]}, snapshot_ms?}
Repo journal    journal/<sha1(repo)[:16]>.jsonl, one line per closed call that changed something:
  {ts, session, agent_id, call, t0, t1, paths}
"""
import hashlib
import json
import os
import time

from . import changes, limits, state
from .adapters import extract_paths

TTL = 86400
JOURNAL_MAX = 1 << 20
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _file(ev):
    return state.path("ledger", limits.safe_session_key(ev.agent, ev.session_id) + ".json")


def _update(ev, change):
    """Read-modify-write the session ledger under its lock: parallel subagents share one session."""
    f = _file(ev)
    with state.locked("ledger-" + f.stem):      # not f.stem: that is the session file's lock
        d = state.read_json(f) or {}
        d.setdefault("pending", {})
        d.setdefault("calls", [])
        change(d)
        cutoff = time.time() - TTL
        d["pending"] = {k: v for k, v in d["pending"].items() if v["t0"] >= cutoff}
        d["calls"] = [c for c in d["calls"] if c["t1"] >= cutoff]
        state.write_json(f, d)


def _real(paths):
    return [os.path.realpath(p) for p in paths]


def _journal_key(repo):
    return hashlib.sha1(repo.encode()).hexdigest()[:16]


def _journal(repo):
    return state.path("journal", _journal_key(repo) + ".jsonl")


def journal_append(repo, rec):
    """Every session in the repo appends here, so appends and the prune share one lock."""
    p = _journal(repo)
    with state.locked("journal-" + _journal_key(repo)):
        state.append_jsonl(p, rec)
        if p.stat().st_size > JOURNAL_MAX:
            keep = state.read_jsonl(p, time.time() - TTL)
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
            os.replace(tmp, p)


def _tracked(repo, d):
    n = (d.get("tracked") or {}).get(repo)
    return n if n is not None else changes.git(repo, "ls-files", "-z").count("\0")


def _timed_snapshot(repo):
    t = time.monotonic()
    tree = changes.snapshot(repo)
    return tree, (time.monotonic() - t) * 1000


def pre(ev, cfg):
    call = ev.call
    repo = changes.repo_root(ev.cwd)
    rec = {"call": call["id"], "agent_id": call["agent_id"], "tool": call["tool"], "t0": time.time(), "repo": repo}
    tracked = None
    if call["tool"] in FILE_TOOLS:
        rec.update(kind="files", pre=changes.blob_ids(repo, _real(extract_paths(call["input"], ev.cwd))))
    elif not repo:
        return                                       # a Bash call outside any repo: no tree to watch
    else:
        tracked = _tracked(repo, state.read_json(_file(ev)) or {})
        if tracked > cfg["ledger"]["max_tree_files"]:
            rec.update(kind="skipped", pre=None)
        else:
            tree, ms = _timed_snapshot(repo)
            rec.update(kind="tree", pre=tree, snapshot_ms=ms)

    def add(d):
        d["pending"][call["id"]] = rec
        if tracked is not None:
            d.setdefault("tracked", {})[repo] = tracked
    _update(ev, add)


def post(ev, cfg):
    call_id = ev.call["id"]
    p = ((state.read_json(_file(ev)) or {}).get("pending") or {}).get(call_id)
    if not p:                                        # no Pre seen (installed mid-call, or pruned)
        return
    repo, deltas, ms = p["repo"], {}, p.get("snapshot_ms")
    if p["kind"] == "files":
        now = changes.blob_ids(repo, list(p["pre"]))
        deltas = {path: [b, now[path]] for path, b in p["pre"].items() if now[path] != b}
    elif p["kind"] == "tree":
        tree, post_ms = _timed_snapshot(repo)
        ms += post_ms
        deltas = {os.path.join(repo, rel): d for rel, d in changes.tree_deltas(repo, p["pre"], tree).items()}
    closed = {k: v for k, v in p.items() if k not in ("pre", "snapshot_ms")}
    closed.update(t1=time.time(), deltas=deltas)
    if ms is not None:
        closed["snapshot_ms"] = round(ms, 1)

    def close(d):
        d["pending"].pop(call_id, None)
        d["calls"].append(closed)
    _update(ev, close)
    if repo and deltas:
        journal_append(repo, {"ts": closed["t1"], "session": ev.session_id, "agent_id": closed["agent_id"],
                              "call": call_id, "t0": closed["t0"], "t1": closed["t1"], "paths": sorted(deltas)})


def _overlaps(calls):
    """Pairs of calls whose [t0, t1] windows overlap."""
    calls, n = sorted(calls, key=lambda c: c["t0"]), 0
    for i, a in enumerate(calls):
        for b in calls[i + 1:]:
            if b["t0"] >= a["t1"]:
                break
            n += 1
    return n


def shadow(ev, cfg, s, artifacts):
    """One shadow.jsonl line comparing this session's ledger with today's whole-tree diff at Stop."""
    repo, started = s.get("repo"), s["started"]
    d = state.read_json(_file(ev)) or {}
    calls = [c for c in d.get("calls", []) if c["t1"] >= started]
    today = sorted({os.path.realpath(os.path.join(repo or "", a["path"])) for a in artifacts})
    current = changes.blob_ids(repo, today)
    mine = {}
    for c in calls:
        for path, (_, post_blob) in c["deltas"].items():
            mine.setdefault(path, set()).add(post_blob)
    others = set()
    if repo:
        for r in state.read_jsonl(_journal(repo), started):
            if r["session"] != ev.session_id:
                others.update(r["paths"])
    own = [p for p in today if current.get(p) in mine.get(p, ())]
    foreign = [p for p in today if p not in own and p in others]
    state.append_jsonl(state.path("shadow.jsonl"), {
        "ts": time.time(), "session": ev.session_id, "turn_id": s.get("turn_id"), "repo": repo, "today": today,
        "own": own, "foreign": foreign, "unattributed": [p for p in today if p not in own and p not in foreign],
        "ledger_only": sorted(set(mine) - set(today)), "pending_open": len(d.get("pending", {})),
        "calls": len(calls), "skipped": sum(c["kind"] == "skipped" for c in calls), "overlaps": _overlaps(calls),
        "bash_snapshot_ms": [c["snapshot_ms"] for c in calls if "snapshot_ms" in c]})
