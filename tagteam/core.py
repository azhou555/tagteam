"""Hook phase handlers: turn-start snapshots, touch ledger, turn-end review, plan gate."""
import os
import time
import uuid

from . import changes, classify, limits, live, quota, router, runner, sandbox, state, verdict
from .adapters import MARKER


def _session_file(ev):
    return state.path("sessions", f"{limits.safe_session_key(ev.agent, ev.session_id)}.json")


def turn_start(ev, cfg):
    if ev.prompt.lstrip().startswith(MARKER):      # Copilot re-submits our block message as a prompt
        return None
    repo = changes.repo_root(ev.cwd)
    snap = changes.snapshot(repo) if repo else None
    with state.locked(_session_file(ev).stem):
        state.write_json(_session_file(ev), {
            "turn_id": ev.turn_id or uuid.uuid4().hex[:12], "started": time.time(), "cwd": ev.cwd,
            "repo": repo, "baseline": snap, "turn_baseline": snap,    # baseline advances each round; turn_baseline never
            "refs": changes.refs(repo) if repo else {},
            "prompt": ev.prompt, "round": 0, "touched": [], "reviewed_digests": {}, "prior": [],
            "plan_rounds": 0, "plan_prior": [], "outside_touched": [], "reverted_prior": [],
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
        repo = s.get("repo")        # never reset: PostToolUse can't see the pre-edit state, so these can't prove a revert
        s["outside_touched"] = sorted(set(s.get("outside_touched", [])) | {
            p for p in ev.paths if not changes.in_repo(p, repo)})
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
    outside = [p for p in s["touched"] if not changes.in_repo(p, repo)]
    arts += changes.outside_changes(outside, s["reviewed_digests"], ignore)
    return arts, head


def headroom_map(cfg):
    """Quota mode only, and only for listed agents: ordered mode never probes (Copilot's probe is `gh api`)."""
    if cfg["routing"]["mode"] != "quota":
        return {}
    agents = cfg["routing"]["quota"]["agents"]
    return {a: quota.headroom(a, cfg) for a in dict.fromkeys(cfg["reviewers"].get("use") or []) if a in agents}


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


STATE_ERROR = limits.STATE_ERROR
SLOW_SETUP = "repo too slow to sandbox, see tagteam.log"


def _clock():
    return time.monotonic()


def _run_record(ev, cfg, s, run_tag, round_no, confirm, **kw):
    """The runs.jsonl line for a run with no verdict yet. `prior` counts the issues open going in."""
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:4]
    return {"ts": time.time(), "run_id": run_id, "author": ev.agent, "session_id": ev.session_id, "tag": run_tag,
            "round": round_no, "max_rounds": cfg["limits"]["max_rounds_per_turn"], "prior": len(s.get("prior") or []),
            "confirm": confirm, "reviewers": [], "action": "allow", "blocking": 0, "advisory": 0, "failed": [],
            "duration": 0.0, **kw}


def review(ev, cfg, s, artifacts, round_no, run_tag, lock, confirm=False, rebuttal=False, started=None):
    """Guard, route, run, decide. The caller holds the session lock (`lock` says how that went). Returns
    (action, message, run_dir, open_issues); message is the block reason or, on allow, a one-line completion
    notice. open_issues are the findings still open afterwards: the new blocking ones, or `s["prior"]` when no
    reviewer gave a verdict. run_dir None means no run was spawned: a block is a re-issued one, an allow with
    open issues is a re-issued block on the confirm round, an allow with a message and none is a state-error
    notice, and an allow without a message is a silent skip. `confirm` marks the review after the last fix
    round, which can never block. `rebuttal` marks a re-review of an unchanged diff against the author's reply.
    `started` is the `_clock()` reading when the hook began work; setup since then comes out of the deadline."""
    if lock != limits.HELD:
        state.log("session lock failed, not reviewing", author=ev.agent, session=ev.session_id)
        run = _run_record(ev, cfg, s, run_tag, round_no, confirm, reason=STATE_ERROR)
        live.record_run(run)
        return "allow", completion(run, 0), None, []
    return _review(ev, cfg, s, artifacts, round_no, run_tag, confirm, rebuttal, _clock() if started is None else started)


def _session_guard(ev, handler, *args):
    """Run handler(*args, lock=...) holding this session's lock; a second trigger for the session is skipped."""
    with limits.session_lock(ev.agent, ev.session_id) as lock:
        if lock == limits.BUSY:
            state.log("review already running for this session; skipped", author=ev.agent, session=ev.session_id)
            return None
        return handler(*args, lock)


def _review(ev, cfg, s, artifacts, round_no, run_tag, confirm, rebuttal, started):
    now = time.time()
    rv = cfg["review"]
    groups = {}
    annotated = classify.annotate(artifacts, cfg)
    for a in annotated:
        groups.setdefault(a["kind"], []).append(a)
    key, dig = _session_file(ev).stem, limits.digest(annotated, ev.last_message if rebuttal else "")
    seen = limits.remembered(key, dig)
    if seen:
        state.log("same diff already reviewed; not reviewing again", author=ev.agent, action=seen["action"], round=round_no)
        if seen["action"] == "block":                # no reviewer spawned, but the status must show this round
            final = confirm                          # the last word can't block: record what is still open
            run = _run_record(ev, cfg, s, run_tag, round_no, confirm, action="allow" if final else "block",
                              blocking=len(seen["blocking"]))
            live.record_run(run)
            if final:
                return "allow", completion(run, 0), None, seen["blocking"]
            return "block", seen["message"], None, seen["blocking"]
        return "allow", None, None, []
    unreadable = [f for f, v in (("disabled.json", limits.disabled()), ("cooldowns.json", quota.cooldowns())) if v is None]
    if unreadable:                                   # a safety list we can't read fails closed
        state.log("reviewer safety state unreadable; not reviewing (tagteam doctor --clear resets it)", files=unreadable)
        hm, (plan, reason) = {}, ({}, STATE_ERROR)
    else:
        hm = headroom_map(cfg)
        plan, reason = router.route(ev.agent, groups, cfg, hm, load_map(now))
    listed = cfg["reviewers"].get("use") or []
    stray = [r for r in plan if r not in listed]
    if stray:                                        # last line of defence: only the user's list ever sees code
        state.log("router planned unlisted reviewers; dropped", reviewers=stray)
        plan = {r: ts for r, ts in plan.items() if r in listed}
        reason = reason or (None if plan else "no listed reviewer is available")
    run = _run_record(ev, cfg, s, run_tag, round_no, confirm, ts=now)
    run_id = run["run_id"]
    refused = plan and limits.reserve(run_id, f"{ev.agent}:{ev.session_id}", now, cfg)
    if refused:
        plan, reason = {}, refused
    run["reviewers"] = list(plan)
    run_dir = state.path("runs", run_id, "x").parent
    run_dir.mkdir(parents=True, exist_ok=True)
    request = {"run_id": run_id, "tag": run_tag, "author": ev.agent, "session_id": ev.session_id,
               "repo": s.get("repo"), "round": round_no, "intent": s.get("prompt", ""), "headroom": hm,
               "assignments": {r: [{"kind": t["kind"], "lens": t["lens"], "paths": [a["path"] for a in t["artifacts"]]}
                               for t in ts] for r, ts in plan.items()},
               "artifacts": [{k: a[k] for k in ("path", "kind", "status", "lines_changed", "risk")} for a in artifacts]}
    state.write_json(run_dir / "request.json", request)
    if reason:
        run["reason"] = reason
    lines_changed = sum(a["lines_changed"] for a in artifacts)
    if not plan:
        state.log("not reviewed", run_id=run_id, reason=reason, headroom=hm)
        live.record_run(run)
        return "allow", completion(run, lines_changed), run_dir, s.get("prior") or []
    repo = s.get("repo")
    git_activity = changes.ref_activity(repo, s["refs"], changes.refs(repo)) if repo and "refs" in s else []
    outside = sorted({p for p in s.get("touched", []) if os.path.isabs(p)} | {a["path"] for a in artifacts if os.path.isabs(a["path"])})
    outside = [p for p in outside if not changes.in_repo(p, repo)]
    before = changes.snapshot(repo) if repo else None
    watch = sandbox.Watch(repo, before, outside)
    with sandbox.checkouts(repo, before, list(plan), repo or ev.cwd, max(7200, 2 * rv["deadline_s"])) as works:   # one disposable tree each
        setup = _clock() - started                   # snapshots, stale sweep, worktree adds: all inside the hook timeout
        if setup > min(60, rv["deadline_s"] / 2):
            state.log("repo too slow to sandbox; not reviewing", run_id=run_id, setup_s=round(setup, 1))
            run.update(reviewers=[], reason=SLOW_SETUP)
            live.record_run(run)
            return "allow", completion(run, lines_changed), run_dir, s.get("prior") or []
        deadline = rv["deadline_s"] - setup
        jobs = {r: runner.build_prompt(r, ev.agent, works[r], s.get("prompt", ""), ts, rv["max_diff_chars"],
                                       prior=s.get("prior") or None, author_reply=ev.last_message,
                                       git_activity=git_activity)
                for r, ts in plan.items()}
        if repo:                                     # intent, replies and findings may quote the real path
            jobs = {r: p.replace(repo, works[r]) for r, p in jobs.items()}
        with live.tracking({"run_id": run_id, "author": ev.agent, "session_id": ev.session_id, "tag": run_tag,
                            "round": round_no, "max_rounds": cfg["limits"]["max_rounds_per_turn"], "reviewers": list(plan),
                            "started": now, "expires": now + deadline + 60}) as update:
            outcomes = runner.run_all(jobs, cfg, works, run_dir, deadline, on_done=update)
        guilty = {r: "wrote in its sandbox" for r in plan if works[r] != (repo or ev.cwd) and sandbox.dirty(works[r], before)}
    seen = watch.after()
    in_place = any(works[r] == (repo or ev.cwd) for r in plan)
    if seen["git"]:                                  # a security signal; users rarely edit these mid-review
        guilty = {r: "git config/hooks changed" for r in plan}
    elif in_place and (seen["repo"]["tree"] or seen["outside"]):   # no sandbox to prove who wrote: postcondition decides
        guilty = {r: "real tree or touched files changed with no sandbox" for r in plan}
    changed = seen["repo"]["tree"] + seen["repo"]["refs"] + seen["outside"]
    if guilty:
        state.log("REVIEWER WROTE OUTSIDE READ-ONLY; disabled", run_id=run_id, reviewers=list(guilty),
                  why=guilty, git=seen["git"], outside=seen["outside"], tree=seen["repo"]["tree"][:50],
                  refs=seen["repo"]["refs"])
        for r, why in guilty.items():
            limits.disable(r, f"{why} (run {run_id})")
        run["disabled"] = list(guilty)
    if changed:
        state.log("repo changed during review", run_id=run_id, blamed=list(guilty), tree=seen["repo"]["tree"][:50],
                  refs=seen["repo"]["refs"], outside=seen["outside"])
        run["real_repo_changed"] = changed
    warn = ""
    if guilty:
        warn += f" · ⚠ disabled {', '.join(guilty)}: wrote outside read-only (tagteam doctor --clear)"
    if changed:
        warn += " · ⚠ your repo changed during review (see tagteam.log)"
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
        limits.remember(key, dig, action, blocking, msg, s.get("turn_id"))
    state.write_json(run_dir / "verdict.json", {"action": action, "blocking": blocking, "advisory": advisory,
                                                "failed": failed, "message": msg})
    run["usage"] = {r: meta["usage"] for r, (meta, _) in outcomes.items() if meta.get("usage")}
    run.update(action=action, blocking=len(blocking), advisory=len(advisory), failed=failed,
               duration=round(time.time() - now, 1))
    live.record_run(run)
    open_issues = blocking if len(failed) < len(results) else s.get("prior") or []
    return action, (msg if action == "block" else completion(run, lines_changed)) + warn, run_dir, open_issues


def wanted(ev, cfg, artifacts):
    """Author and trigger filters: False means this change is nobody's business, so no run and no notice."""
    if ev.agent not in cfg["authors"]["review"]:
        return False
    return bool({a["kind"] for a in classify.annotate(artifacts, cfg)} & set(cfg["triggers"]["kinds"]))


def _write_back(sf, turn_id, update):
    """update(session) and save it, unless a new turn has started since the review began: its fresh session
    state wins, and the verdict reaches the author anyway."""
    with state.locked(sf.stem):
        cur = state.read_json(sf)
        if not cur:
            return
        if cur.get("turn_id") != turn_id:
            state.log("new turn started during review; session state not updated", session=sf.stem,
                      reviewed_turn=turn_id, current_turn=cur.get("turn_id"))
            return
        update(cur)
        state.write_json(sf, cur)


def turn_end(ev, cfg):
    return _session_guard(ev, _turn_end, ev, cfg)


def _turn_end(ev, cfg, lock):
    started = _clock()
    sf = _session_file(ev)
    s = state.read_json(sf)
    if not s:
        return None                                  # hooks installed mid-session: no baseline yet
    m = cfg["limits"]["max_rounds_per_turn"]
    artifacts, head = collect(s, cfg)
    changed = sum(a["lines_changed"] for a in artifacts)
    prior = s.get("prior") or []
    if not prior and changed and s.get("reverted_prior"):
        prior = s["reverted_prior"]                  # a redo of reverted work: the issues are open again
    round_no = s["round"] + 1
    confirm = round_no == m + 1 and bool(prior)      # the last word on the issues still open; it can't block
    if round_no > m and not confirm:
        return None
    whole = None                                     # the turn's whole change so far, for rebuttals and the revert check
    if prior and ev.agent in cfg["authors"]["review"]:
        ignore, repo = cfg["review"]["ignore"], s.get("repo")
        outside = changes.outside_changes(s.get("outside_touched", []), {}, ignore)
        if repo and s.get("turn_baseline") and head:
            whole = changes.diff_trees(repo, s["turn_baseline"], head, ignore) + outside
            if not whole and not changes.ref_activity(repo, s.get("refs", {}), changes.refs(repo)):
                return _reverted(ev, cfg, s, sf, round_no, head)
        elif outside:
            whole = outside
    rebuttal = changed == 0 and bool(prior)
    if rebuttal:                                     # no fix: the author may be answering, so recheck the turn's whole diff
        if not whole:
            state.log("rebuttal not re-checked (nothing to diff)", author=ev.agent, session=ev.session_id)
            return None
        artifacts, changed = whole, sum(a["lines_changed"] for a in whole)
    # Round 1 skips trivial edits; later rounds re-check any fix, however small.
    elif changed == 0 or (s["round"] == 0 and changed < cfg["triggers"]["min_changed_lines"]):
        return None
    if not wanted(ev, cfg, artifacts):
        return None
    action, msg, run_dir, open_issues = review(ev, cfg, s, artifacts, round_no, "turn", lock, confirm, rebuttal, started)
    if run_dir is None and action == "allow" and not open_issues:   # no round spent: diff already passed, or a state-error notice
        return {"action": action, "message": msg} if msg else None

    def advance(cur):
        cur["round"] = round_no
        cur["prior"], cur["reverted_prior"] = open_issues, []
        if head:
            cur["baseline"] = head                   # next round reviews only the fix delta
        for a in artifacts:
            if os.path.isabs(a["path"]):
                cur["reviewed_digests"][a["path"]] = changes.file_digest(a["path"])
        cur["touched"] = []
    _write_back(sf, s.get("turn_id"), advance)
    state.log("review", run=str(run_dir), action=action, author=ev.agent, round=round_no)
    return {"action": action, "message": msg}


def _reverted(ev, cfg, s, sf, round_no, head):
    """The author undid everything it changed this turn: the open issues are gone and no review is spent."""
    run = _run_record(ev, cfg, s, "turn", round_no, False, reason="reverted")
    live.record_run(run)
    _write_back(sf, s.get("turn_id"), lambda cur: cur.update(      # not a round: a redo hits the blocked digest, which counts
        prior=[], reverted_prior=cur.get("prior") or cur.get("reverted_prior") or [], baseline=head, touched=[]))
    state.log("changes reverted; open issues resolved without a review", author=ev.agent, round=round_no)
    return {"action": "allow", "message": completion(run, 0)}


def plan_gate(ev, cfg):
    """Review a plan before the user approves it (Claude ExitPlanMode)."""
    return _session_guard(ev, _plan_gate, ev, cfg)


def _plan_gate(ev, cfg, lock):
    started = _clock()
    if not ev.plan_text.strip():
        return None
    s = state.read_json(_session_file(ev)) or {"prompt": "", "repo": changes.repo_root(ev.cwd), "prior": []}
    art = {"path": "(proposed plan)", "kind": "plan", "status": "added",
           "lines_changed": ev.plan_text.count("\n") + 1, "diff": ev.plan_text}
    if not wanted(ev, cfg, [art]):
        return None
    prior, m = s.get("plan_prior") or [], cfg["limits"]["max_rounds_per_turn"]
    gate_rounds = s.get("plan_rounds", 0) + 1
    confirm = gate_rounds == m + 1 and bool(prior)
    if gate_rounds > m and not confirm:
        return None
    action, msg, run_dir, open_issues = review(ev, cfg, {**s, "prior": prior}, [art], gate_rounds, "plan-gate", lock,
                                               confirm, started=started)

    def settle(cur):                                 # findings carry only from a block to the next try at the same plan
        cur["plan_rounds"], cur["plan_prior"] = (0, []) if action == "allow" else (gate_rounds, open_issues)
    _write_back(_session_file(ev), s.get("turn_id"), settle)
    if run_dir is None and action == "allow" and not open_issues and not msg:
        return None
    return {"action": action, "message": msg}


HANDLERS = {"turn-start": turn_start, "touch": touch, "turn-end": turn_end, "plan-gate": plan_gate}
