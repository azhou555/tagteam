"""Ownership ledger (Claude): which tool call changed which file. At Stop it labels each changed file own, foreign or
unattributed, and only own files can block. The whole-tree diff still decides what is reviewed.

core calls every entry point through a guard: a failure is logged and the review goes on as if every change were
the session's own. See docs/attribution-redesign.md.

Session ledger  ledger/claude-<sid>.json  {"pending": {call: pending}, "calls": [closed], "tracked": {repo: n},
                                          "slow": {repo: true}, "unbracketed": time of the last Post with no Pre}
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
SHADOW_MAX = 20 << 20
SHADOW_TTL = 8 * 86400                               # Phase C reads a week of these
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


def _norm(path):
    """Resolve the directories (macOS /var is /private/var), not the file itself: a symlink stays the link."""
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _real(paths):
    return [_norm(p) for p in paths]


def _journal_key(repo):
    return hashlib.sha1(repo.encode()).hexdigest()[:16]


def _journal(repo):
    return state.path("journal", _journal_key(repo) + ".jsonl")


def _append_pruned(p, lock, rec, limit, ttl=TTL):
    """Many sessions append to p, so the append and the prune share one lock."""
    with state.locked(lock):
        state.append_jsonl(p, rec)
        if p.stat().st_size > limit:
            keep = state.read_jsonl(p, time.time() - ttl)
            tmp = p.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in keep))
            os.replace(tmp, p)


def journal_append(repo, rec):
    _append_pruned(_journal(repo), "journal-" + _journal_key(repo), rec, JOURNAL_MAX)


def _sweep_dead_ledgers():
    """A ledger prunes itself only when its session writes: drop the ones no session has touched for a day."""
    cutoff = time.time() - TTL
    for f in state.path("ledger", "x").parent.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


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
        d = state.read_json(_file(ev)) or {}
        tracked = _tracked(repo, d)
        if tracked > cfg["ledger"]["max_tree_files"] or (d.get("slow") or {}).get(repo):
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
    if not p:                                        # no Pre hook ran: installed mid-call, or hooks older than the package
        _update(ev, lambda d: d.update(unbracketed=time.time()))
        return
    repo, deltas, ms, slow = p["repo"], {}, p.get("snapshot_ms"), False
    if p["kind"] == "files":
        now = changes.blob_ids(repo, list(p["pre"]))
        deltas = {path: [b, now[path]] for path, b in p["pre"].items() if now[path] != b}
    elif p["kind"] == "tree":
        tree, post_ms = _timed_snapshot(repo)
        slow = max(ms, post_ms) > cfg["ledger"]["max_snapshot_ms"]   # untracked data the file count can't see
        ms += post_ms
        deltas = {os.path.join(repo, rel): d for rel, d in changes.tree_deltas(repo, p["pre"], tree).items()}
    closed = {k: v for k, v in p.items() if k not in ("pre", "snapshot_ms")}
    closed.update(t1=time.time(), deltas=deltas)
    if ms is not None:
        closed["snapshot_ms"] = round(ms, 1)

    def close(d):
        d["pending"].pop(call_id, None)
        d["calls"].append(closed)
        if p["kind"] == "tree" and slow:
            d.setdefault("slow", {})[repo] = True
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


def _spans(calls, path):
    """[[pre, post]] of this session's calls on `path`, calls overlapping in time merged into one span: parallel
    subagents' before and after readings interleave, so only the span's ends are comparable."""
    spans = []
    for c in sorted((c for c in calls if path in c["deltas"]), key=lambda c: c["t0"]):
        pre, post = c["deltas"][path]
        if spans and c["t0"] < spans[-1][2]:
            if c["t1"] >= spans[-1][2]:
                spans[-1][1:] = [post, c["t1"]]
        else:
            spans.append([pre, post, c["t1"]])
    return [s[:2] for s in spans]


def _chain(spans, base, now):
    """True when the session's spans lead unbroken from `base` to `now`: nobody else wrote in between."""
    return bool(spans) and spans[0][0] == base and spans[-1][1] == now and all(
        b[0] == a[1] for a, b in zip(spans, spans[1:]))


def _window(ev, since):
    d = state.read_json(_file(ev)) or {}
    return d, [c for c in d.get("calls", []) if c["t1"] >= since]


def unreviewed(ev, repo, since):
    """True when this session has tool calls since `since` that wrote under `repo` or may still be writing."""
    d, calls = _window(ev, since)
    return any(changes.in_repo(p, repo) for c in calls for p in c["deltas"]) or any(
        p["t0"] >= since and p.get("repo") and changes.in_repo(p["repo"], repo) for p in d.get("pending", {}).values())


def owners(ev, cfg, s, base, since, head, artifacts):
    """{artifact path: own | foreign | unattributed} for the repo diff `base` -> `head`, or None when the ledger can't
    vouch for this window and every change counts as the session's own, as before the ledger. Logs one shadow.jsonl
    line either way."""
    repo = s.get("repo")
    d, calls = _window(ev, since)
    in_repo = [a for a in artifacts if not os.path.isabs(a["path"])]
    blobs = changes.tree_deltas(repo, base, head) if repo and base and head and in_repo else {}
    others = set()
    if repo:
        for r in state.read_jsonl(_journal(repo), since):
            if r["session"] != ev.session_id:
                others.update(r["paths"])
    labels, mine = {}, set()
    for c in calls:
        mine.update(c["deltas"])
    for a in in_repo:
        path = _norm(os.path.join(repo, a["path"]))
        b, now = blobs.get(a["path"], [None, None])
        labels[a["path"]] = ("own" if _chain(_spans(calls, path), b, now) else
                             "foreign" if path in others else "unattributed")
    skipped = sum(c["kind"] == "skipped" for c in calls)
    trusted = repo and not skipped and d.get("unbracketed", 0) < since    # a call with no Pre wrote unseen
    by = lambda label: sorted(_norm(os.path.join(repo, p)) for p, v in labels.items() if v == label)
    today = sorted(_norm(os.path.join(repo or "", a["path"])) for a in in_repo)
    _sweep_dead_ledgers()
    _append_pruned(state.path("shadow.jsonl"), "shadow", {
        "ts": time.time(), "session": ev.session_id, "turn_id": s.get("turn_id"), "repo": repo, "today": today,
        "own": by("own"), "foreign": by("foreign"), "unattributed": by("unattributed"),
        "ledger_only": sorted(mine - set(today)),
        "fallback": not trusted,
        "pending_open": sum(v["t0"] >= since for v in d.get("pending", {}).values()),
        "calls": len(calls), "skipped": skipped, "overlaps": _overlaps(calls),
        "bash_snapshot_ms": [c["snapshot_ms"] for c in calls if "snapshot_ms" in c]}, SHADOW_MAX, SHADOW_TTL)
    if not trusted:
        return None
    return {**labels, **{a["path"]: "own" for a in artifacts if os.path.isabs(a["path"])}}


def worktree_artifacts(ev, s, since, ignore):
    """(artifacts, labels) for what this session's calls changed in git worktrees inside its repo, such as an
    `isolation: worktree` subagent's. The repo's own diff skips them as nested repos (F3)."""
    repo = s.get("repo")
    _, calls = _window(ev, since)
    calls = [c for c in calls if c["repo"] and c["repo"] != repo and changes.in_repo(c["repo"], repo)]
    arts, labels = [], {}
    paths = sorted({p for c in calls for p in c["deltas"]})
    now = changes.blob_ids(repo, paths) if paths else {}
    for path in paths:
        spans = _spans(calls, path)
        rel = os.path.relpath(path, repo)
        if spans[0][0] == now[path] or changes.ignored(rel, ignore):
            continue
        lines, diff = changes.blob_diff(repo, rel, spans[0][0], now[path])
        if lines is None:
            continue
        arts.append({"path": rel, "status": "added" if spans[0][0] is None else "deleted" if now[path] is None
                     else "modified", "lines_changed": lines, "diff": diff})
        labels[rel] = "own" if _chain(spans, spans[0][0], now[path]) else "unattributed"
    return arts, labels
