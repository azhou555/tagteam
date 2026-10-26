"""Build reviewer prompts, run the reviewer CLIs in parallel (read-only), parse Findings."""
import concurrent.futures as cf
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import tomllib

from . import quota, state

RUBRICS = tomllib.loads((state.ROOT / "prompts" / "rubrics.toml").read_text())
SCHEMA_PATH = state.ROOT / "schema" / "findings.schema.json"
QUOTA_ERR = re.compile(r"rate.?limit|quota|usage limit|too many requests|\b429\b|insufficient (credits|balance)|"
                       r"limit (reached|exceeded)|out of credits", re.I)
SEVERITIES = ("blocker", "major", "minor", "nit")
# agy headless auto-denies shell commands and then returns an empty response, so steer it off them.
AGY_NOTE = ("\nIMPORTANT: shell/terminal commands are disabled in this session and any attempt aborts the review. "
            "Do not run commands (no git, cat, ls, grep). The diff above has what you need; use only file-viewing "
            "tools if you must read more.")


def build_prompt(reviewer, author, repo, intent, tasks, max_chars, prior=None, author_reply="", git_activity=()):
    parts = [RUBRICS["preamble"].format(reviewer=reviewer, author=author, repo=repo)]
    if intent:
        parts.append(f"The user's request this turn:\n<request>\n{intent[:4000]}\n</request>")
    if git_activity:
        parts.append("## Git activity this turn\nThe diffs below show only working-tree changes. The author also moved "
                     "these git refs (already done: commits, history rewrites, and pushes, which show up as "
                     "refs/remotes/* moves). Don't report as missing work that this already shows.\n"
                     + "\n".join(f"- {line}" for line in git_activity)[:4000])
    n_arts = sum(len(t["artifacts"]) for t in tasks) or 1
    budget = max_chars // n_arts
    for t in tasks:
        parts.append(f"\n## Review task: {t['kind']}\n{RUBRICS['kind'][t['kind']]}\n{RUBRICS['lens'][t['lens']]}")
        for a in t["artifacts"]:
            d = a["diff"]
            if len(d) > budget:
                d = d[:budget] + f"\n... [diff truncated; read {a['path']} for the rest]"
            parts.append(f"\n### {a['path']} ({a['status']}, {a['lines_changed']} lines changed)\n{d}")
    if prior:
        parts.append("\n## Round 2\nYou (or a peer) raised these findings last round. The author has since changed "
                     "the work. Re-check each: drop it if resolved or if the author's rebuttal is convincing; keep it "
                     "only if it still holds.\n" + json.dumps(prior, indent=1))
        if author_reply:
            parts.append(f"Author's reply:\n<reply>\n{author_reply[:4000]}\n</reply>")
    parts.append(RUBRICS["output"])
    return "\n".join(parts)


def command(agent, cfg, prompt, workdir, deadline):
    """Returns (argv, stdin_text, output_file or None). Every flag set here was verified read-only."""
    model = cfg["agents"][agent].get("model")
    out = os.path.join(workdir, "out.json")
    if agent == "claude":
        argv = ["claude", "-p", "--output-format", "json", "--json-schema", SCHEMA_PATH.read_text(),
                "--permission-mode", "plan", "--disallowedTools", "Write", "Edit", "NotebookEdit"]
        return argv + (["--model", model] if model else []), prompt, None
    if agent == "codex":
        # approval_policy=never matters: with the user's auto_review approver, read-only alone let apply_patch through.
        argv = ["codex", "exec", "-s", "read-only", "-c", 'approval_policy="never"', "--skip-git-repo-check",
                "--output-schema", str(SCHEMA_PATH), "-o", out]
        return argv + (["-m", model] if model else []) + ["-"], prompt, out
    if agent == "copilot":
        argv = ["copilot", "-p", prompt, "-s", "--no-ask-user", "--allow-all-tools", "--deny-tool", "write",
                "--deny-tool", "shell", "--usage-output-file", os.path.join(workdir, "usage.json")]
        return argv + (["--model", model] if model else []), None, None
    if agent == "agy":
        argv = ["agy", "-p", prompt + AGY_NOTE, "--mode", "plan", "--output-format", "json", "--json-schema", str(SCHEMA_PATH),
                "--print-timeout", f"{deadline}s"]
        effort = cfg["agents"][agent].get("effort")
        return argv + (["--model", model] if model else []) + (["--effort", effort] if effort else []), None, None
    raise ValueError(agent)


def extract_json(text):
    text = (text or "").strip()
    for candidate in (text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            pass
    return None


def parse_output(agent, stdout, out_file):
    if out_file and os.path.exists(out_file):
        data = extract_json(open(out_file).read())
    else:
        data = extract_json(stdout)
    if isinstance(data, dict) and agent in ("claude", "agy"):
        so = data.get("structured_output")
        data = so if isinstance(so, dict) else extract_json(data.get("result") or data.get("response") or "")
    return normalize(data)


def normalize(data):
    if not isinstance(data, dict) or "findings" not in data:
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim"):
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
        try:
            conf = float(f.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        findings.append({"severity": sev, "file": str(f.get("file") or ""), "line": f.get("line"),
                         "claim": str(f["claim"]), "evidence": str(f.get("evidence") or ""),
                         "suggested_fix": str(f.get("suggested_fix") or ""), "confidence": conf})
    verdict = data.get("verdict") if data.get("verdict") in ("approve", "changes_requested") else (
        "changes_requested" if findings else "approve")
    return {"verdict": verdict, "summary": str(data.get("summary") or ""), "findings": findings}


def _spawn(argv, stdin, cwd, timeout):
    proc = subprocess.Popen(argv, cwd=cwd, env={**os.environ, "TAGTEAM_DEPTH": "1"}, text=True,
                            start_new_session=True, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
        return stdout, stderr, proc.returncode, False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        return stdout, stderr, proc.returncode, True


def run_one(agent, cfg, prompt, cwd, deadline, run_dir):
    workdir = tempfile.mkdtemp(prefix=f"{agent}-", dir=run_dir)
    t0 = time.time()
    for attempt in (1, 2):
        argv, stdin, out_file = command(agent, cfg, prompt, workdir, int(deadline - (time.time() - t0)))
        stdout, stderr, rc, timed_out = _spawn(argv, stdin, cwd, deadline - (time.time() - t0))
        result = None if timed_out else parse_output(agent, stdout, out_file)
        retryable = agent == "agy" and '"denied_actions"' in (stdout or "") and deadline - (time.time() - t0) > 60
        if result is not None or not retryable:
            break
        prompt += "\n\nYour previous attempt ran a shell command, was denied, and produced nothing. Do not run any."
    rec = {"reviewer": agent, "ok": result is not None, "timed_out": timed_out, "rc": rc, "attempts": attempt,
           "duration": round(time.time() - t0, 1)}
    usage = state.read_json(os.path.join(workdir, "usage.json"))
    if usage:
        rec["cost"] = usage.get("totalPremiumRequestCost", 1)
    if result is None:
        tail = (stderr or "")[-2000:] + (stdout or "")[-2000:]
        rec["error"] = tail[-500:]
        if QUOTA_ERR.search(tail):
            rec["quota_error"] = True
            quota.cooldown(agent, cfg["routing"]["cooldown_min"] * 4)
        else:
            quota.cooldown(agent, cfg["routing"]["cooldown_min"])
    state.write_json(os.path.join(workdir, "result.json"), {"meta": rec, "result": result,
                                                             "stdout_tail": (stdout or "")[-4000:],
                                                             "stderr_tail": (stderr or "")[-4000:]})
    return rec, result


def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
    """jobs: {reviewer: prompt}. Runs in parallel; returns {reviewer: (meta, result)}.

    on_done(reviewer, meta) is called from this thread as each reviewer finishes.
    """
    out = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(run_one, a, cfg, p, cwd, deadline, run_dir): a for a, p in jobs.items()}
        for f in cf.as_completed(futs):
            out[futs[f]] = f.result()
            if on_done:
                on_done(futs[f], out[futs[f]][0])
    return out
