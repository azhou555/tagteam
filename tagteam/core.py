"""Hook phase handlers: turn-start snapshots, touch ledger, turn-end review, plan gate."""
import os
import time
import uuid

from . import changes, classify, quota, router, runner, state, verdict
from .adapters import MARKER


def _session_file(ev):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in ev.session_id or "nosession")
    return state.path("sessions", f"{ev.agent}-{safe}.json")


def turn_start(ev, cfg):
    if ev.prompt.lstrip().startswith(MARKER):      # Copilot re-submits our block message as a prompt
        return None
    repo = changes.repo_root(ev.cwd)
    with state.locked(_session_file(ev).stem):
        state.write_json(_session_file(ev), {
            "turn_id": ev.turn_id or uuid.uuid4().hex[:12], "started": time.time(), "cwd": ev.cwd,
            "repo": repo, "baseline": changes.snapshot(repo) if repo else None,
            "prompt": ev.prompt, "round": 0, "touched": [], "reviewed_digests": {}, "prior": [],
        })
    return None


def touch(ev, cfg):
    if not ev.paths:
        return None
    with state.locked(_session_file(ev).stem):
        s = state.read_json(_session_file(ev))
        if not s:
            return None
        s["touched"] = sorted(set(s["touched"]) | set(ev.paths))
        state.write_json(_session_file(ev), s)
    return None


def collect(s, cfg):
    """This turn's artifacts: git diff since last review point + touched files outside the repo."""
    ignore = cfg["review"]["ignore"]
    arts, head = [], None
    repo = s.get("repo")
    if repo and s.get("baseline"):
        head = changes.snapshot(repo)
        arts = changes.diff_trees(repo, s["baseline"], head, ignore)
    outside = [p for p in s["touched"] if not (repo and os.path.abspath(p).startswith(repo + os.sep))]
    arts += changes.outside_changes(outside, s["reviewed_digests"], ignore)
    return arts, head


def headroom_map(cfg):
    return {a: quota.headroom(a, cfg) for a in cfg["agents"]}


def load_map(now):
    load = {}
    for r in state.read_jsonl(state.path("history.jsonl"), now - 86400):
        load[r["reviewer"]] = load.get(r["reviewer"], 0) + 1
    return load


def review(ev, cfg, s, artifacts, round_no, run_tag):
    """Route, run, decide. Returns (action, message, run_dir, blocking)."""
    now = time.time()
    rv = cfg["review"]
    groups = {}
    for a in classify.annotate(artifacts, cfg):
        groups.setdefault(a["kind"], []).append(a)
    hm = headroom_map(cfg)
    plan = router.route(ev.agent, groups, cfg, hm, load_map(now))
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    run_dir = state.path("runs", run_id, "x").parent
    run_dir.mkdir(parents=True, exist_ok=True)
    request = {"run_id": run_id, "tag": run_tag, "author": ev.agent, "session_id": ev.session_id,
               "repo": s.get("repo"), "round": round_no, "intent": s.get("prompt", ""), "headroom": hm,
               "assignments": {r: [{"kind": t["kind"], "lens": t["lens"], "paths": [a["path"] for a in t["artifacts"]]}
                               for t in ts] for r, ts in plan.items()},
               "artifacts": [{k: a[k] for k in ("path", "kind", "status", "lines_changed", "risk")} for a in artifacts]}
    state.write_json(run_dir / "request.json", request)
    if not plan:
        state.log("no eligible reviewers", run_id=run_id, headroom=hm)
        return "allow", None, run_dir, []
    cwd = s.get("repo") or ev.cwd
    jobs = {r: runner.build_prompt(r, ev.agent, cwd, s.get("prompt", ""), ts, rv["max_diff_chars"],
                                   prior=s.get("prior") or None, author_reply=ev.last_message)
            for r, ts in plan.items()}
    before = changes.snapshot(s["repo"]) if s.get("repo") else None
    outcomes = runner.run_all(jobs, cfg, cwd, run_dir, rv["deadline_s"])
    if before and changes.snapshot(s["repo"]) != before:
        state.log("WORKING TREE CHANGED DURING REVIEW", run_id=run_id, reviewers=list(plan))
    with state.locked("history"):
        for r, (meta, _) in outcomes.items():
            state.append_jsonl(state.path("history.jsonl"),
                               {"ts": now, "run_id": run_id, "author": ev.agent, **meta,
                                "kinds": [t["kind"] for t in plan[r]]})
    results = {r: res for r, (_, res) in outcomes.items()}
    failed = [r for r, res in results.items() if res is None]
    action, blocking, advisory = verdict.decide(results, cfg, round_no)
    msg = verdict.message(plan, blocking, advisory, round_no, rv["max_rounds"], failed)
    state.write_json(run_dir / "verdict.json", {"action": action, "blocking": blocking, "advisory": advisory,
                                                "failed": failed, "message": msg})
    return action, msg, run_dir, blocking


def turn_end(ev, cfg):
    sf = _session_file(ev)
    s = state.read_json(sf)
    if not s:
        return None                                  # hooks installed mid-session: no baseline yet
    rv = cfg["review"]
    artifacts, head = collect(s, cfg)
    changed = sum(a["lines_changed"] for a in artifacts)
    # Round 1 skips trivial edits; later rounds re-check any fix, however small.
    if changed == 0 or (s["round"] == 0 and changed < rv["min_changed_lines"]):
        return None
    round_no = s["round"] + 1
    if round_no > rv["max_rounds"]:
        return None
    action, msg, run_dir, blocking = review(ev, cfg, s, artifacts, round_no, "turn")
    with state.locked(sf.stem):
        s = state.read_json(sf) or s
        s["round"] = round_no
        s["prior"] = blocking
        if head:
            s["baseline"] = head                     # next round reviews only the fix delta
        for a in artifacts:
            if os.path.isabs(a["path"]):
                s["reviewed_digests"][a["path"]] = changes.file_digest(a["path"])
        s["touched"] = []
        state.write_json(sf, s)
    state.log("review", run=str(run_dir), action=action, author=ev.agent, round=round_no)
    return {"action": "block", "message": msg} if action == "block" else None


def plan_gate(ev, cfg):
    """Review a plan before the user approves it (Claude ExitPlanMode)."""
    if not ev.plan_text.strip():
        return None
    s = state.read_json(_session_file(ev)) or {"prompt": "", "repo": changes.repo_root(ev.cwd), "prior": []}
    art = {"path": "(proposed plan)", "kind": "plan", "status": "added",
           "lines_changed": ev.plan_text.count("\n") + 1, "diff": ev.plan_text}
    gate_rounds = s.get("plan_rounds", 0) + 1
    if gate_rounds > cfg["review"]["max_rounds"]:
        return None
    action, msg, _, _ = review(ev, cfg, {**s, "prior": []}, [art], gate_rounds, "plan-gate")
    with state.locked(_session_file(ev).stem):
        cur = state.read_json(_session_file(ev))
        if cur:
            cur["plan_rounds"] = gate_rounds
            state.write_json(_session_file(ev), cur)
    return {"action": "block", "message": msg} if action == "block" else None


HANDLERS = {"turn-start": turn_start, "touch": touch, "turn-end": turn_end, "plan-gate": plan_gate}
