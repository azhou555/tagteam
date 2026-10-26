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

from . import harnesses, limits, quota, resources, state

RUBRICS = tomllib.loads(resources.text("rubrics.toml"))
QUOTA_ERR = re.compile(r"rate.?limit|quota|usage limit|too many requests|\b429\b|insufficient (credits|balance)|"
                       r"limit (reached|exceeded)|out of credits", re.I)


def _tilde(path):
    """Out-of-repo artifacts are absolute paths; show them ~-relative so no prompt carries the home path."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


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
            shown = _tilde(a["path"])
            d = a["diff"].replace(a["path"], shown)
            if len(d) > budget:
                d = d[:budget] + f"\n... [diff truncated; read {shown} for the rest]"
            parts.append(f"\n### {shown} ({a['status']}, {a['lines_changed']} lines changed)\n{d}")
    if prior:
        parts.append("\n## Round 2\nYou (or a peer) raised these findings last round. The author has since changed "
                     "the work. Re-check each: drop it if resolved or if the author's rebuttal is convincing; keep it "
                     "only if it still holds.\n" + json.dumps(prior, indent=1))
        if author_reply:
            parts.append(f"Author's reply:\n<reply>\n{author_reply[:4000]}\n</reply>")
    parts.append(RUBRICS["output"])
    return "\n".join(parts)


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
    reviewer, options = harnesses.get(agent).reviewer, cfg["reviewers"].get(agent, {})
    for attempt in (1, 2):
        argv, stdin, out_file = reviewer.argv(prompt, options, workdir, int(deadline - (time.time() - t0)))
        stdout, stderr, rc, timed_out = _spawn(argv, stdin, cwd, deadline - (time.time() - t0))
        result = None if timed_out else reviewer.parse(stdout, out_file)
        retryable = agent == "agy" and '"denied_actions"' in (stdout or "") and deadline - (time.time() - t0) > 60
        if result is not None or not retryable:
            break
        prompt += "\n\nYour previous attempt ran a shell command, was denied, and produced nothing. Do not run any."
    rec = {"reviewer": agent, "ok": result is not None, "timed_out": timed_out, "rc": rc, "attempts": attempt,
           "duration": round(time.time() - t0, 1)}
    usage = reviewer.usage(stdout, workdir)
    if "premium_requests" in usage:
        rec["cost"] = usage["premium_requests"]
    if result is not None:
        limits.note_result(agent, True, cfg)
    else:
        tail = (stderr or "")[-2000:] + (stdout or "")[-2000:]
        rec["error"] = tail[-500:]
        if QUOTA_ERR.search(tail):
            rec["quota_error"] = True
            quota.cooldown(agent, cfg["limits"]["cooldown_min"] * 4)
        else:
            limits.note_result(agent, False, cfg)
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
