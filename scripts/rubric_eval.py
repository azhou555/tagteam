"""Replay past Copilot reviews under two rubric versions and compare what each reports.

    python3 scripts/rubric_eval.py extract OUT [--n 10]    # past runs whose Copilot review raised major+ findings
    python3 scripts/rubric_eval.py run OUT [--old HEAD]    # re-run Copilot on each case, old and new rubric
    python3 scripts/rubric_eval.py report OUT              # OUT/report.md; reads labels from OUT/labels.md

The prompts come from Copilot's own session logs (~/.copilot/session-state), matched to tagteam runs by time and
content. Each case is re-run in a worktree of the review-time snapshot when git still has it, else of HEAD; both
rubric versions always get the same tree. `run` spends 2 Copilot premium requests per case and is resumable.
Only reads ~/.tagteam; never writes tagteam's state.
"""
import argparse
import concurrent.futures as cf
import difflib
import glob
import json
import os
import pathlib
import re
import statistics
import subprocess
import sys
import time
import tomllib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tagteam import changes, config, core, harnesses, runner, sandbox, state, verdict  # noqa: E402

RUNS = state.HOME / "state" / "runs"
SESSIONS = pathlib.Path.home() / ".copilot" / "session-state"
HEADER = re.compile(r"You are (\w+), reviewing work that another AI coding agent \((\w+)\)")
BODY_STARTS = ("The user's request this turn:\n", "## Git activity this turn\n", "\n## Review task: ")
OUTPUT_START = "Respond with ONLY a JSON object"
SECTION = re.compile(r"^### (.+?) \((?:added|modified|deleted|renamed)[^)]*lines changed\)$", re.M)
RANK = verdict.RANK


def _epoch(run_id):
    return time.mktime(time.strptime(run_id.split("-")[0], "%Y%m%dT%H%M%S"))


def _copilot_prompts():
    """[(start_epoch, sandbox_cwd, prompt)] for every Copilot session that was a tagteam review."""
    out = []
    for f in glob.glob(str(SESSIONS / "*" / "events.jsonl")):
        start = cwd = prompt = None
        for line in open(f):
            e = json.loads(line)
            if e["type"] == "session.start":
                start = datetime.fromisoformat(e["data"]["startTime"].replace("Z", "+00:00")).timestamp()
                cwd = e["data"].get("context", {}).get("cwd")
            elif e["type"] == "user.message":
                prompt = e["data"]["content"]
                break
        if start and prompt and HEADER.match(prompt):
            out.append((start, cwd, prompt))
    return out


def _recorded(run_dir):
    for p in glob.glob(str(run_dir / "copilot-*" / "result.json")):
        return (json.load(open(p)).get("result") or {}).get("findings") or []
    return None


def extract(out, n):
    prompts = _copilot_prompts()
    cases = []
    for req_path in sorted(RUNS.glob("*/request.json"), reverse=True):
        req = json.load(open(req_path))
        repo, tasks = req.get("repo"), req.get("assignments", {}).get("copilot")
        if not tasks or not repo or repo.startswith("/private/") or not os.path.isdir(repo):
            continue
        found = _recorded(req_path.parent)
        if not found or not any(RANK[f["severity"]] <= RANK["major"] for f in found):
            continue
        t0, paths = _epoch(req["run_id"]), [p for t in tasks for p in t["paths"]]
        match = [(s, c, p) for s, c, p in prompts if t0 - 5 <= s <= t0 + 300
                 and req.get("intent", "")[:200] in p and all(f"### {_tilde(x)} (" in p for x in paths)]
        if len(match) != 1:
            continue
        _, cwd, prompt = match[0]
        cases.append({"run_id": req["run_id"], "repo": repo, "intent": req.get("intent", ""), "round": req["round"],
                      "sandbox": cwd, "prompt": prompt, "recorded": found})
        if len(cases) == n:
            break
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "cases.jsonl", "w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    print(f"{len(cases)} cases -> {out / 'cases.jsonl'}")


def _tilde(path):
    return runner._tilde(path)


def _rubric(rev):
    if rev == "WORKING":
        return runner.RUBRICS
    text = subprocess.run(["git", "show", f"{rev}:tagteam/data/rubrics.toml"], capture_output=True, text=True,
                          check=True, cwd=pathlib.Path(__file__).resolve().parent.parent).stdout
    return tomllib.loads(text)


def rewrite(prompt, rubric, old_root, new_root):
    """The same review prompt with `rubric`'s preamble, kind, lens and output text, rooted at new_root."""
    reviewer, author = HEADER.match(prompt).groups()
    i = min(k for k in (prompt.find(m) for m in BODY_STARTS) if k >= 0)
    j = prompt.rfind(OUTPUT_START)
    lines = prompt[i:j].split("\n")
    for n, line in enumerate(lines):
        for v in list(rubric["kind"].values()) + list(rubric["lens"].values()):
            head = v.split(":")[0] if not v.startswith("Your lens") else v.split(".")[0]
            if line != v and line.startswith(head + (":" if not v.startswith("Your lens") else ".")):
                lines[n] = v
    body = "\n".join(lines)
    for root in {old_root, old_root.removeprefix("/private")}:
        body = core.swap_root(body, root, new_root)
    return rubric["preamble"].format(reviewer=reviewer, author=author, repo=new_root) + "\n" + body + rubric["output"]


def _snapshot_commit(case):
    """The review-time snapshot commit if git still has it (unreachable `tagteam-review` commits), else None."""
    repo = case["repo"]
    fsck = changes.git(repo, "fsck", "--unreachable", "--no-reflogs", check=False)
    shas = [l.split()[2] for l in fsck.splitlines() if l.startswith("unreachable commit")]
    cands = [s for s in shas if changes.git(repo, "log", "-1", "--format=%s", s, check=False).strip() == "tagteam-review"]
    added = {}
    for m in SECTION.finditer(case["prompt"]):
        rel = m.group(1)
        if rel.startswith(("~", "/")):
            continue
        nxt = SECTION.search(case["prompt"], m.end())
        chunk = case["prompt"][m.end(): nxt.start() if nxt else len(case["prompt"])]
        added[rel] = [l[1:].strip() for l in chunk.split("\n") if l.startswith("+") and not l.startswith("+++")
                      and len(l.strip()) > 8]
    best, score = None, 0.0
    for c in cands:
        hit = total = 0
        for rel, want in added.items():
            text = changes.git(repo, "show", f"{c}:{rel}", check=False)
            hit += sum(w in text for w in want)
            total += len(want)
        s = hit / total if total else 0
        if s > score:
            best, score = c, s
    return best if score >= 0.95 else None


def _run_variant(case_dir, case, variant, rubric, cwd, opts):
    vdir = case_dir / variant
    if (vdir / "result.json").exists():
        return
    vdir.mkdir(parents=True, exist_ok=True)
    prompt = rewrite(case["prompt"], rubric, case["sandbox"], cwd)
    (vdir / "prompt.txt").write_text(prompt)
    rv = harnesses.get("copilot").reviewer
    argv, stdin, out_file = rv.argv(prompt, opts, str(vdir), 300)
    t0 = time.time()
    stdout, stderr, rc, timed_out = runner._spawn(argv, stdin, cwd, 300)
    result = None if timed_out else rv.parse(stdout, out_file)
    json.dump({"result": result, "rc": rc, "timed_out": timed_out, "duration": round(time.time() - t0, 1),
               "stdout_tail": (stdout or "")[-4000:], "stderr_tail": (stderr or "")[-2000:]},
              open(vdir / "result.json", "w"), indent=1)
    print(f"{case['run_id']} {variant}: {'ok' if result else 'FAILED'} in {time.time() - t0:.0f}s", flush=True)


def _run_case(out, case, rubrics, opts):
    case_dir = out / case["run_id"]
    case_dir.mkdir(exist_ok=True)
    if all((case_dir / v / "result.json").exists() for v in rubrics):
        return
    commit = _snapshot_commit(case)
    tree = changes.git(case["repo"], "rev-parse", f"{commit or 'HEAD'}^{{tree}}").strip()
    (case_dir / "tree.json").write_text(json.dumps({"snapshot": bool(commit), "tree": tree}))
    with sandbox.checkout(case["repo"], tree) as cwd:
        if cwd == os.getcwd() or cwd == case["repo"]:
            raise RuntimeError(f"no sandbox for {case['run_id']}; refusing to review in place")
        with cf.ThreadPoolExecutor(len(rubrics)) as ex:
            list(ex.map(lambda kv: _run_variant(case_dir, case, kv[0], kv[1], cwd, opts), rubrics.items()))


def run(out, old, jobs):
    cases = [json.loads(l) for l in open(out / "cases.jsonl")]
    rubrics = {"old": _rubric(old), "new": _rubric("WORKING")}
    opts = config.load()["reviewers"].get("copilot", {})
    with cf.ThreadPoolExecutor(jobs) as ex:
        for f in cf.as_completed([ex.submit(_run_case, out, c, rubrics, opts) for c in cases]):
            if f.exception():
                print("case failed:", repr(f.exception()), flush=True)


def _labels(out):
    """{finding key: 'real'|'noise'} from labels.md lines `- [real] <key> ...` / `- [noise] <key> ...`."""
    p = out / "labels.md"
    if not p.exists():
        return {}
    return {m.group(2): m.group(1) for m in re.finditer(r"^- \[(real|noise)\] (\S+)", p.read_text(), re.M)}


def _key(case, i):
    return f"{case['run_id']}#{i}"


def _refound(f, findings):
    """Loose: the same file and a nearby line, or a similar claim. Reruns rephrase, so this is approximate."""
    for g in findings:
        if g["file"] == f["file"] and (abs((g["line"] or 0) - (f["line"] or 0)) <= 8 or
                                       difflib.SequenceMatcher(None, g["claim"].lower(), f["claim"].lower()).ratio() > 0.45):
            return g
    return None


def _row(f):
    loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
    return f"| {f['severity']} | {f['confidence']:.2f} | `{loc}` | {f['claim'][:160].replace('|', '/')} |"


def report(out):
    cfg = config.load()
    cases = [json.loads(l) for l in open(out / "cases.jsonl")]
    labels = _labels(out)
    stats = {v: {"blocks": 0, "sev": {k: 0 for k in RANK}, "conf": [], "failed": 0, "real": 0, "noise": 0}
             for v in ("recorded", "old", "new")}
    lines, label_lines = ["# Rubric replay", ""], ["# Label each recorded finding: replace `[ ]` with `[real]` or "
                                                   "`[noise]`", ""]
    for case in cases:
        d = out / case["run_id"]
        tree = json.loads((d / "tree.json").read_text()) if (d / "tree.json").exists() else {}
        res = {"recorded": case["recorded"]}
        for v in ("old", "new"):
            r = json.load(open(d / v / "result.json")) if (d / v / "result.json").exists() else {}
            res[v] = (r.get("result") or {}).get("findings") if r.get("result") else None
        lines += [f"## {case['run_id']} · {case['repo']}",
                  f"Request: {case['intent'][:200]!r} · round {case['round']} · tree: "
                  f"{'review-time snapshot' if tree.get('snapshot') else 'HEAD (snapshot gone)'}", ""]
        for v, fs in res.items():
            s = stats[v]
            if fs is None:
                s["failed"] += 1
                lines += [f"**{v}**: no result", ""]
                continue
            action, _, _ = verdict.decide({"copilot": {"findings": [dict(f) for f in fs]}}, cfg, 1)
            s["blocks"] += action == "block"
            for f in fs:
                s["sev"][f["severity"]] += 1
                s["conf"].append(f["confidence"])
            lines += [f"**{v}**: {action}, {len(fs)} findings", "", "| sev | conf | where | claim |", "|---|---|---|---|"]
            lines += [_row(f) for f in fs] + [""]
        for i, f in enumerate(case["recorded"]):
            k = _key(case, i)
            if RANK[f["severity"]] > RANK["major"]:
                continue
            label_lines.append(f"- [{labels.get(k, ' ')}] {k} [{f['severity']}] `{f['file']}:{f['line']}` {f['claim']}"
                               + (f"\n  Evidence: {f['evidence'][:300]}" if f.get("evidence") else ""))
            for v in ("old", "new"):
                g = res[v] and _refound(f, res[v])
                if g and RANK[g["severity"]] <= RANK["major"] and k in labels:
                    stats[v][labels[k]] += 1
            if k in labels:
                stats["recorded"][labels[k]] += 1
    head = ["| | recorded | old rubric | new rubric |", "|---|---|---|---|"]
    for name, fn in [("would block", lambda s: s["blocks"]), ("no result", lambda s: s["failed"]),
                     *[(sev, lambda s, sev=sev: s["sev"][sev]) for sev in RANK],
                     ("median confidence", lambda s: f"{statistics.median(s['conf']):.2f}" if s["conf"] else "-"),
                     ("confidence < 0.6", lambda s: sum(c < 0.6 for c in s["conf"])),
                     ("labeled real, kept at major+", lambda s: s["real"]),
                     ("labeled noise, kept at major+", lambda s: s["noise"])]:
        head.append(f"| {name} | " + " | ".join(str(fn(stats[v])) for v in ("recorded", "old", "new")) + " |")
    (out / "report.md").write_text("\n".join(lines[:2] + [f"{len(cases)} cases", ""] + head + [""] + lines[2:]))
    if not (out / "labels.md").exists():
        (out / "labels.md").write_text("\n".join(label_lines) + "\n")
    print("\n".join(head))
    print(f"\n{out / 'report.md'}" + ("" if labels else f"\nunlabeled: fill in {out / 'labels.md'}, then report again"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["extract", "run", "report"])
    ap.add_argument("out", type=pathlib.Path)
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--old", default="HEAD", help="git rev of the old rubrics.toml (default HEAD)")
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args()
    {"extract": lambda: extract(a.out, a.n), "run": lambda: run(a.out, a.old, a.jobs),
     "report": lambda: report(a.out)}[a.cmd]()


if __name__ == "__main__":
    main()
