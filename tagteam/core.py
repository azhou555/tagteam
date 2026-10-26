"""Hook phase handlers: turn-start snapshots, touch ledger, turn-end review, plan gate."""
import os
import time
import uuid

from . import changes, classify, limits, live, quota, router, runner, sandbox, state, verdict
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
            "refs": changes.refs(repo) if repo else {},
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
    return {a: quota.headroom(a, cfg) for a in cfg["routing"]["quota"]["agents"]}


def load_map(now):
    load = {}
    for r in state.read_jsonl(state.path("history.jsonl"), now - 86400):
        load[r["reviewer"]] = load.get(r["reviewer"], 0) + 1
    return load


def completion(run, lines_changed):
    """One line for the user when a review lets the turn through."""
    done = [r for r in run["reviewers"] if r not in run["failed"]]
    msg = f"{MARKER} {live.summary(run)}"
    if done:
        msg += f" · {', '.join(done)} reviewed {lines_changed} changed lines in {run['duration']:.0f}s"
    return msg + (f" · unavailable: {', '.join(run['failed'])}" if run["failed"] else "")


STATE_ERROR = "state error, see tagteam.log"


def review(ev, cfg, s, artifacts, round_no, run_tag, lock):
    """Guard, route, run, decide. The caller holds the session lock (`lock` says how that went). Returns
    (action, message, run_dir, blocking); message is the block reason or, on allow, a one-line completion
    notice. run_dir None means no run was spawned: a block is a re-issued one, an allow with a message is a
    state-error notice, and an allow without one is a silent skip."""
    if lock != limits.HELD:
        state.log("session lock failed, not reviewing", author=ev.agent, session=ev.session_id)
        run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
        run = {"ts": time.time(), "run_id": run_id, "author": ev.agent, "session_id": ev.session_id, "tag": run_tag,
               "round": round_no, "reviewers": [], "action": "allow", "blocking": 0, "advisory": 0, "failed": [],
               "duration": 0.0, "reason": STATE_ERROR}
        live.record_run(run)
        return "allow", completion(run, 0), None, []
    return _review(ev, cfg, s, artifacts, round_no, run_tag)


def _session_guard(ev, handler, *args):
    """Run handler(*args, lock=...) holding this session's lock; a second trigger for the session is skipped."""
    with limits.session_lock(ev.agent, ev.session_id) as lock:
        if lock == limits.BUSY:
            state.log("review already running for this session; skipped", author=ev.agent, session=ev.session_id)
            return None
        return handler(*args, lock)


def _review(ev, cfg, s, artifacts, round_no, run_tag):
    now = time.time()
    rv = cfg["review"]
    groups = {}
    annotated = classify.annotate(artifacts, cfg)
    for a in annotated:
        groups.setdefault(a["kind"], []).append(a)
    key, dig = _session_file(ev).stem, limits.digest(annotated)
    seen = limits.remembered(key, dig)
    if seen:
        state.log("same diff already reviewed; not reviewing again", author=ev.agent, action=seen["action"], round=round_no)
        if seen["action"] == "block":
            return "block", seen["message"], None, seen["blocking"]
        return "allow", None, None, []
    hm = headroom_map(cfg)
    plan, reason = router.route(ev.agent, groups, cfg, hm, load_map(now))
    listed = cfg["reviewers"].get("use") or []
    stray = [r for r in plan if r not in listed]
    if stray:                                        # last line of defence: only the user's list ever sees code
        state.log("router planned unlisted reviewers; dropped", reviewers=stray)
        plan = {r: ts for r, ts in plan.items() if r in listed}
        reason = reason or (None if plan else "no listed reviewer is available")
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    if plan and not limits.reserve(run_id, f"{ev.agent}:{ev.session_id}", now, cfg):
        plan, reason = {}, limits.limit_reason(cfg)
    run_dir = state.path("runs", run_id, "x").parent
    run_dir.mkdir(parents=True, exist_ok=True)
    request = {"run_id": run_id, "tag": run_tag, "author": ev.agent, "session_id": ev.session_id,
               "repo": s.get("repo"), "round": round_no, "intent": s.get("prompt", ""), "headroom": hm,
               "assignments": {r: [{"kind": t["kind"], "lens": t["lens"], "paths": [a["path"] for a in t["artifacts"]]}
                               for t in ts] for r, ts in plan.items()},
               "artifacts": [{k: a[k] for k in ("path", "kind", "status", "lines_changed", "risk")} for a in artifacts]}
    state.write_json(run_dir / "request.json", request)
    run = {"ts": now, "run_id": run_id, "author": ev.agent, "session_id": ev.session_id, "tag": run_tag,
           "round": round_no, "reviewers": list(plan), "action": "allow", "blocking": 0, "advisory": 0,
           "failed": [], "duration": 0.0}
    if reason:
        run["reason"] = reason
    lines_changed = sum(a["lines_changed"] for a in artifacts)
    if not plan:
        state.log("not reviewed", run_id=run_id, reason=reason, headroom=hm)
        live.record_run(run)
        return "allow", completion(run, lines_changed), run_dir, []
    repo = s.get("repo")
    git_activity = changes.ref_activity(repo, s["refs"], changes.refs(repo)) if repo and "refs" in s else []
    outside = sorted({p for p in s.get("touched", []) if os.path.isabs(p)} | {a["path"] for a in artifacts if os.path.isabs(a["path"])})
    outside = [p for p in outside if not (repo and p.startswith(repo + os.sep))]
    digests, before = {p: changes.file_digest(p) for p in outside}, changes.snapshot(repo) if repo else None
    with sandbox.checkout(repo, before, repo or ev.cwd) as work:     # reviewers see the snapshot, never the real path
        jobs = {r: runner.build_prompt(r, ev.agent, work, s.get("prompt", ""), ts, rv["max_diff_chars"],
                                       prior=s.get("prior") or None, author_reply=ev.last_message,
                                       git_activity=git_activity)
                for r, ts in plan.items()}
        with live.tracking({"run_id": run_id, "author": ev.agent, "session_id": ev.session_id, "tag": run_tag,
                            "round": round_no, "max_rounds": cfg["limits"]["max_rounds_per_turn"], "reviewers": list(plan),
                            "started": now, "expires": now + rv["deadline_s"] + 60}) as update:
            outcomes = runner.run_all(jobs, cfg, work, run_dir, rv["deadline_s"], on_done=update)
    if sandbox.wrote(repo, before, digests):         # can't tell which reviewer wrote, so none stay trusted
        state.log("REVIEWER WROTE TO THE REAL TREE; disabling this round's reviewers", run_id=run_id, reviewers=list(plan))
        for r in plan:
            limits.disable(r, f"wrote to the real tree during run {run_id}")
        run["wrote_real_tree"] = True
    with state.locked("history"):
        for r, (meta, _) in outcomes.items():
            state.append_jsonl(state.path("history.jsonl"),
                               {"ts": now, "run_id": run_id, "author": ev.agent, **meta,
                                "kinds": [t["kind"] for t in plan[r]]})
    results = {r: res for r, (_, res) in outcomes.items()}
    failed = [r for r, res in results.items() if res is None]
    action, blocking, advisory = verdict.decide(results, cfg, round_no)
    msg = verdict.message(plan, blocking, advisory, round_no, cfg["limits"]["max_rounds_per_turn"], failed)
    if len(failed) < len(results):                   # a run with no verdict must not suppress a retry
        limits.remember(key, dig, action, blocking, msg)
    state.write_json(run_dir / "verdict.json", {"action": action, "blocking": blocking, "advisory": advisory,
                                                "failed": failed, "message": msg})
    run.update(action=action, blocking=len(blocking), advisory=len(advisory), failed=failed,
               duration=round(time.time() - now, 1))
    live.record_run(run)
    return action, msg if action == "block" else completion(run, lines_changed), run_dir, blocking


def wanted(ev, cfg, artifacts):
    """Author and trigger filters: False means this change is nobody's business, so no run and no notice."""
    if ev.agent not in cfg["authors"]["review"]:
        return False
    return bool({a["kind"] for a in classify.annotate(artifacts, cfg)} & set(cfg["triggers"]["kinds"]))


def turn_end(ev, cfg):
    return _session_guard(ev, _turn_end, ev, cfg)


def _turn_end(ev, cfg, lock):
    sf = _session_file(ev)
    s = state.read_json(sf)
    if not s:
        return None                                  # hooks installed mid-session: no baseline yet
    rv = cfg["review"]
    artifacts, head = collect(s, cfg)
    changed = sum(a["lines_changed"] for a in artifacts)
    # Round 1 skips trivial edits; later rounds re-check any fix, however small.
    if changed == 0 or (s["round"] == 0 and changed < cfg["triggers"]["min_changed_lines"]):
        return None
    if not wanted(ev, cfg, artifacts):
        return None
    round_no = s["round"] + 1
    if round_no > cfg["limits"]["max_rounds_per_turn"]:
        return None
    action, msg, run_dir, blocking = review(ev, cfg, s, artifacts, round_no, "turn", lock)
    if run_dir is None and action == "allow":        # no round spent: diff already passed, or a state-error notice
        return {"action": action, "message": msg} if msg else None
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
    return {"action": action, "message": msg}


def plan_gate(ev, cfg):
    """Review a plan before the user approves it (Claude ExitPlanMode)."""
    return _session_guard(ev, _plan_gate, ev, cfg)


def _plan_gate(ev, cfg, lock):
    if not ev.plan_text.strip():
        return None
    s = state.read_json(_session_file(ev)) or {"prompt": "", "repo": changes.repo_root(ev.cwd), "prior": []}
    art = {"path": "(proposed plan)", "kind": "plan", "status": "added",
           "lines_changed": ev.plan_text.count("\n") + 1, "diff": ev.plan_text}
    if not wanted(ev, cfg, [art]):
        return None
    gate_rounds = s.get("plan_rounds", 0) + 1
    if gate_rounds > cfg["limits"]["max_rounds_per_turn"]:
        return None
    action, msg, run_dir, _ = review(ev, cfg, {**s, "prior": []}, [art], gate_rounds, "plan-gate", lock)
    if run_dir is None and action == "allow":
        return {"action": action, "message": msg} if msg else None
    with state.locked(_session_file(ev).stem):
        cur = state.read_json(_session_file(ev))
        if cur:
            cur["plan_rounds"] = gate_rounds
            state.write_json(_session_file(ev), cur)
    return {"action": action, "message": msg}


HANDLERS = {"turn-start": turn_start, "touch": touch, "turn-end": turn_end, "plan-gate": plan_gate}
