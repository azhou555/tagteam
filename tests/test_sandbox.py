import dataclasses
import itertools
import json
import os
import shutil
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from tests.helpers import HookRig, finding, make_repo, sh, state, use_toml, write_user_config

from tagteam import changes, config, core, harnesses, limits, router, runner, sandbox
from tagteam.harnesses.base import Reviewer, no_usage

APPROVE = {"verdict": "approve", "summary": "", "findings": []}
BLOCK = {"verdict": "request_changes", "summary": "", "findings": [finding("blocker")]}


def fake_harness(script, result=APPROVE):
    """A reviewer 'CLI' that is just a shell script, standing in for harnesses.get."""
    rev = Reviewer(argv=lambda prompt, options, workdir, deadline, cwd: (["sh", "-c", script], None, None),
                   parse=lambda out, f: result, usage=no_usage)
    real = harnesses.get
    return lambda name: dataclasses.replace(real(name), reviewer=rev) if name == "copilot" else real(name)


def worktrees(repo):
    out = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.startswith("worktree ")]


from tagteam.adapters import HookEvent  # noqa: E402


class Checkout(unittest.TestCase):
    def test_sandbox_works_without_git_identity(self):
        repo = make_repo()
        tree = changes.snapshot(repo)
        empty = tempfile.mkdtemp()
        env = {"HOME": empty, "XDG_CONFIG_HOME": empty, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        with mock.patch.dict(os.environ, env):
            for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
                os.environ.pop(k, None)
            with sandbox.checkout(repo, tree) as path:
                self.assertEqual(pathlib.Path(path, "a.py").read_text(), "x = 1\n")
                self.assertNotEqual(os.path.realpath(path), repo)

    def test_checkout_shows_the_snapshot_and_cleans_up_on_error(self):
        repo = make_repo()
        pathlib.Path(repo, "new.py").write_text("y = 2\n")
        tree = changes.snapshot(repo)
        with self.assertRaises(RuntimeError):
            with sandbox.checkout(repo, tree) as path:
                self.assertEqual(pathlib.Path(path, "new.py").read_text(), "y = 2\n")
                self.assertEqual(len(worktrees(repo)), 2)
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(worktrees(repo)), 1)

    def test_sandbox_turns_off_copilots_repo_hooks_without_counting_as_a_write(self):
        repo = make_repo()
        pathlib.Path(repo, ".github/copilot").mkdir(parents=True)
        pathlib.Path(repo, ".github/copilot/settings.local.json").write_text('{"model": "x", "disableAllHooks": false}')
        tree = changes.snapshot(repo)
        with sandbox.checkout(repo, tree) as path:
            got = json.loads(pathlib.Path(path, sandbox.COPILOT_SETTINGS).read_text())
            self.assertEqual(got, {"model": "x", "disableAllHooks": True, "disabledMcpServers": []})
            self.assertEqual(sandbox.dirty(path, tree), [])
            pathlib.Path(path, "a.py").write_text("x = 2\n")
            self.assertEqual(sandbox.dirty(path, tree), ["a.py"])

    def test_sandbox_turns_off_the_repos_copilot_mcp_servers(self):
        repo = make_repo()
        pathlib.Path(repo, ".github/copilot").mkdir(parents=True)
        pathlib.Path(repo, ".mcp.json").write_text('{"mcpServers": {"db": {"command": "sh"}}}')
        pathlib.Path(repo, ".github/mcp.json").write_text('{"mcpServers": {"web": {}, "db": {}}}')
        pathlib.Path(repo, sandbox.COPILOT_SETTINGS).write_text('{"disabledMcpServers": ["mine"]}')
        tree = changes.snapshot(repo)
        with sandbox.checkout(repo, tree) as path:
            got = json.loads(pathlib.Path(path, sandbox.COPILOT_SETTINGS).read_text())
            self.assertEqual(got["disabledMcpServers"], ["mine", "db", "web"])
            self.assertEqual(sandbox.dirty(path, tree), [])

    def test_sandbox_never_writes_through_a_symlink_out_of_it(self):
        repo, outside = make_repo(), tempfile.mkdtemp()
        os.symlink(outside, os.path.join(repo, ".github"))
        tree = changes.snapshot(repo)
        with sandbox.checkout(repo, tree) as path:
            self.assertNotEqual(os.path.realpath(path), repo)
        self.assertEqual(os.listdir(outside), [])

    def test_worktrees_wait_for_another_process_holding_the_repos_lock(self):
        repo = make_repo()
        tree = changes.snapshot(repo)
        lock = state.path("locks", sandbox._registry_lock(repo) + ".lock")
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys; f = open(sys.argv[1], 'w'); "
                                   "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); sys.stdin.read()", lock],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)                                  # a failed assert must not leave the lock held
        self.assertEqual(holder.stdout.readline(), "held\n")
        got = {}

        def review():
            with sandbox.checkout(repo, tree, cwd=repo) as path:
                got["path"] = path
        t = threading.Thread(target=review, daemon=True)
        t.start()
        time.sleep(0.5)
        self.assertEqual((len(worktrees(repo)), got), (1, {}))       # nothing registered while the other holds it
        holder.stdin.close()
        holder.wait()
        t.join(10)
        self.assertNotEqual(got["path"], repo)
        self.assertEqual(len(worktrees(repo)), 1)
        linked = os.path.join(tempfile.mkdtemp(), "linked")
        sh(repo, "git", "worktree", "add", "-q", "--detach", linked)
        self.assertEqual(sandbox._registry_lock(linked), sandbox._registry_lock(repo))   # one .git/worktrees, one lock

    def test_worktree_failure_falls_back_to_cwd(self):
        repo = make_repo()
        with sandbox.checkout(repo, "0" * 40, cwd=repo) as path:
            self.assertEqual(path, repo)

    def test_each_name_gets_its_own_worktree_and_all_are_removed_if_one_fails(self):
        repo = make_repo()
        tree = changes.snapshot(repo)
        with sandbox.checkouts(repo, tree, ["a", "b", "c"], cwd=repo) as m:
            self.assertEqual(len(set(m.values())), 3)
            self.assertTrue(all(pathlib.Path(p, "a.py").is_file() and p != repo for p in m.values()))
        self.assertEqual(len(worktrees(repo)), 1)
        real, n = sandbox._worktree, itertools.count()

        def flaky(r, c):
            if next(n) == 1:
                raise RuntimeError("boom")
            return real(r, c)
        with mock.patch.object(sandbox, "_worktree", flaky), sandbox.checkouts(repo, tree, ["a", "b", "c"], cwd=repo) as m:
            self.assertEqual(set(m.values()), {repo})
        self.assertEqual(len(worktrees(repo)), 1)
        real_populate, n = sandbox._populate, itertools.count()

        def flaky_populate(p):
            if next(n) == 1:
                raise RuntimeError("boom")
            return real_populate(p)
        with mock.patch.object(sandbox, "_populate", flaky_populate), sandbox.checkouts(repo, tree, ["a", "b", "c"], cwd=repo) as m:
            self.assertEqual(set(m.values()), {repo})
        self.assertEqual(len(worktrees(repo)), 1)

    def test_non_git_checkout_yields_cwd(self):
        d = tempfile.mkdtemp()
        with sandbox.checkout(None, None, cwd=d) as path:
            self.assertEqual(path, d)


class EndToEnd(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig()
        write_user_config(use_toml(["copilot"]))
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))

    def tearDown(self):
        self.unrig()

    def stop(self, script, result=APPROVE, name="feature.py"):
        pathlib.Path(self.repo, name).write_text("a = 1\n" * 20)
        with mock.patch.object(harnesses, "get", fake_harness(script, result)):
            return self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))

    def test_findings_name_repo_paths_and_merge_across_reviewers(self):
        write_user_config(use_toml(["copilot", "agy"]) + "[review.per_change]\nsmall = 2\n")
        self.run_hook("claude", "x", self.claude("UserPromptSubmit",
                                                 prompt=f"edit {self.repo}/feature.py, not {self.repo}-other/x.py"))
        seen = {}

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            seen.update(jobs=jobs, cwd=cwd)
            out = {}
            for r, work in cwd.items():
                shown = work.removeprefix("/private") if r == "agy" else work       # as `pwd` may print it
                f = finding("blocker", file=f"{shown}/feature.py", line=2, claim=f"{shown}/feature.py: off by one")
                out[r] = ({"reviewer": r, "ok": True, "duration": 1},
                          {"verdict": "request_changes", "summary": "", "findings": [{**f, "evidence": f"see {shown}"}]})
            return out
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", run_all):
            out = self.run_hook("claude", "x", self.claude("Stop"))
        self.assertEqual(sorted(seen["jobs"]), ["agy", "copilot"])
        for r, prompt in seen["jobs"].items():
            self.assertIn(f"edit {seen['cwd'][r]}/feature.py", prompt)
            self.assertIn(f"not {self.repo}-other/x.py", prompt)                 # a different path, left alone
        self.assertIn("1. [blocker] feature.py:2: feature.py: off by one  (agy, copilot)", out["reason"])
        self.assertIn("Evidence: see .", out["reason"])
        self.assertNotIn("tagteam-review-", out["reason"])

    def timed_stop(self, setup_s):
        """A Stop whose setup (everything before reviewers start) took `setup_s` on the hook's clock."""
        ticks, deadlines = iter([0.0]), []

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            deadlines.append(deadline)
            self.calls.append(jobs)
            return {r: ({"reviewer": r, "ok": True, "duration": 1}, APPROVE) for r in jobs}
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        with mock.patch.object(core, "_clock", side_effect=lambda: next(ticks, setup_s)), \
                mock.patch.object(runner, "run_all", run_all):
            return self.run_hook("claude", "x", self.claude("Stop")), deadlines

    def test_setup_time_comes_out_of_the_reviewer_deadline(self):
        _, deadlines = self.timed_stop(30.0)
        self.assertEqual(deadlines, [config.load()["review"]["deadline_s"] - 30.0])

    def test_slow_setup_skips_the_review_and_cleans_up(self):
        out, deadlines = self.timed_stop(61.0)
        self.assertEqual((deadlines, self.calls), ([], []))
        self.assertEqual(out["systemMessage"], "[tagteam] ⚠ Not reviewed: repo too slow to sandbox, see tagteam.log")
        self.assertEqual(len(worktrees(self.repo)), 1)
        last = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((last["reason"], last["reviewers"]), (core.SLOW_SETUP, []))

    def test_reviewer_writes_land_in_sandbox_only_and_disable_that_reviewer(self):
        self.stop("echo x > pwned.txt")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pwned.txt")))
        self.assertEqual(list(limits.disabled()), ["copilot"])
        self.assertEqual(limits.disabled()["copilot"]["repo"], self.repo)      # where a canary re-tests it
        self.assertEqual(len(worktrees(self.repo)), 1)
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "status", "--porcelain"], capture_output=True,
                                        text=True).stdout.split(), ["??", "feature.py"])

    def test_clean_reviewer_stays_enabled(self):
        self.stop("cat a.py > /dev/null")
        self.assertEqual(limits.disabled(), {})

    def test_prompt_never_contains_real_checkout_path(self):
        seen = {}

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            seen.update(jobs=jobs, cwd=cwd["copilot"], exists=os.path.isdir(cwd["copilot"]))
            out = {r: ({"reviewer": r, "ok": True, "duration": 1}, APPROVE) for r in jobs}
            for r, (meta, _) in out.items():
                on_done(r, meta)
            return out
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", run_all):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertTrue(seen["jobs"] and seen["exists"])
        self.assertNotEqual(seen["cwd"], self.repo)
        for prompt in seen["jobs"].values():
            self.assertNotIn(self.repo, prompt)
            self.assertIn(seen["cwd"], prompt)
        self.assertFalse(os.path.exists(seen["cwd"]))

    def test_sandbox_write_disables_only_that_reviewer_and_delivers_verdict(self):
        write_user_config(use_toml(["copilot", "codex"]) + "[review.per_change]\nsmall = 2\n")
        real = harnesses.get
        bad = fake_harness("echo x > pwned.txt", BLOCK)("copilot").reviewer
        good = fake_harness("true", APPROVE)("copilot").reviewer
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        get = lambda n: dataclasses.replace(real(n), reviewer={"copilot": bad, "codex": good}.get(n, real(n).reviewer))
        with mock.patch.object(harnesses, "get", get):
            out = self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertEqual(out["decision"], "block")                       # the round's verdict is still delivered
        self.assertIn("disabled copilot: wrote outside read-only (tagteam doctor --clear)", out["reason"])
        self.assertEqual(list(limits.disabled()), ["copilot"])
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["disabled"], ["copilot"])
        limits.clear_disabled()
        self.assertEqual(limits.disabled(), {})
        self.assertEqual(router.candidates("claude", config.load()), ["copilot", "codex"])

    def test_disabled_reviewer_is_skipped_next_round(self):
        self.stop("echo x > pwned.txt")
        self.calls.clear()
        pathlib.Path(self.repo, "more.py").write_text("b = 2\n" * 30)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertEqual(self.calls, [])
        self.assertIn("no listed reviewer is available", state.read_jsonl(state.path("runs.jsonl"))[-1]["reason"])

    def stop_two(self, bad_script, result=APPROVE):
        """copilot runs bad_script, codex is clean; both reviewers are in the round and return `result`."""
        write_user_config(use_toml(["copilot", "codex"]) + "[review.per_change]\nsmall = 2\n")
        real = harnesses.get
        revs = {"copilot": fake_harness(bad_script, result)("copilot").reviewer,
                "codex": fake_harness("true", result)("copilot").reviewer}
        get = lambda n: dataclasses.replace(real(n), reviewer=revs.get(n, real(n).reviewer))
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        with mock.patch.object(harnesses, "get", get):
            return self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))

    def test_git_config_change_stops_the_review_and_disables_nobody(self):
        out = self.stop_two("git config core.hooksPath /evil", BLOCK)
        self.assertEqual(limits.disabled(), {})
        self.assertNotIn("decision", out)                                 # the stopped run's verdict is discarded
        self.assertIn("⚠ Not reviewed: git config or hooks changed during review, see tagteam.log", out["systemMessage"])
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((run["reason"], run.get("disabled")), (core.STOPPED, None))
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "config", "core.hooksPath"], capture_output=True,
                                        text=True).stdout.strip(), "/evil")

    def test_stopped_review_leaves_the_change_for_the_next_turn(self):
        self.stop_two("git config core.hooksPath /evil", BLOCK)
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="next"))
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertIn("feature.py", next(iter(self.calls[-1].values())))

    def test_stopped_review_still_records_reviewer_spend_and_names_nobody_as_reviewed(self):
        out = self.stop("git config tagteam.x 1")
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        spent = [h for h in state.read_jsonl(state.path("history.jsonl")) if h["run_id"] == run["run_id"]]
        self.assertEqual([h["reviewer"] for h in spent], ["copilot"])
        self.assertEqual((run["reviewers"], run["stopped_reviewers"]), ([], ["copilot"]))
        self.assertNotIn("changed lines", out["systemMessage"])

    def test_review_that_passes_after_a_stop_clears_the_kept_baseline(self):
        self.stop("git config tagteam.x 1")
        self.stop("true", name="second.py")                               # same turn: covers both, and passes
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="next"))
        self.calls.clear()
        pathlib.Path(self.repo, "third.py").write_text("c = 3\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        prompt = next(iter(self.calls[-1].values()))
        self.assertIn("third.py", prompt)
        self.assertNotIn("feature.py", prompt)

    def test_three_stops_in_a_row_escalate_the_notice(self):
        for i in range(3):
            self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt=f"turn {i}"))
            out = self.stop(f"git config tagteam.x {i}", name=f"f{i}.py")
        self.assertIn("⚠ 3 reviews in a row stopped in this repo by git config or hook changes", out["systemMessage"])
        self.stop("true", name="ok.py")
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="again"))
        out = self.stop("git config tagteam.x 9", name="g.py")
        self.assertNotIn("in a row", out["systemMessage"])

    def test_git_config_change_is_logged_by_key(self):
        self.stop_two("git config core.hooksPath /evil")
        entry = [e for e in state.read_jsonl(state.path("tagteam.log")) if e["msg"].startswith("review stopped")][-1]
        self.assertIn("+core.hookspath=/evil", entry["git"])

    def test_branch_tracking_config_change_disables_nobody_but_warns(self):
        out = self.stop("git config branch.main.vscode-merge-base origin/main; "
                        "git config branch.feature.remote origin; git config branch.feature.merge refs/heads/feature")
        self.assertEqual(limits.disabled(), {})
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertIn("+branch.feature.remote=origin", run["real_repo_changed"])
        self.assertIn("⚠ your repo changed during review (see tagteam.log)", out["systemMessage"])

    def test_sandbox_write_is_logged_by_file(self):
        self.stop("echo x > pwned.txt")
        self.assertIn("pwned.txt", limits.disabled()["copilot"]["reason"])

    def test_hook_install_in_shared_git_dir_stops_the_review(self):
        out = self.stop_two("echo x > $(git rev-parse --git-common-dir)/hooks/pre-commit")
        self.assertEqual(limits.disabled(), {})
        self.assertIn("⚠ Not reviewed: git config or hooks changed during review", out["systemMessage"])

    def test_guilty_reviewer_and_repo_change_show_both_notices(self):
        out = self.stop(f"echo x > pwned.txt; echo y > {self.repo}/user.txt")
        self.assertEqual(list(limits.disabled()), ["copilot"])
        self.assertIn("user.txt", state.read_jsonl(state.path("runs.jsonl"))[-1]["real_repo_changed"])
        self.assertIn("⚠ disabled copilot", out["systemMessage"])
        self.assertIn("⚠ your repo changed during review", out["systemMessage"])

    def test_user_edit_during_review_disables_nobody_but_warns(self):
        out = self.stop(f"echo x > {self.repo}/user.txt; git -C {self.repo} update-ref refs/heads/other HEAD")
        self.assertEqual(limits.disabled(), {})
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertIn("user.txt", run["real_repo_changed"])
        self.assertIn("refs/heads/other", run["real_repo_changed"])
        self.assertNotIn("disabled", run)
        self.assertIn("⚠ your repo changed during review (see tagteam.log)", out["systemMessage"])

    def test_remote_ref_moves_are_ignored(self):
        self.stop(f"git -C {self.repo} update-ref refs/remotes/origin/main HEAD")
        self.assertEqual(limits.disabled(), {})
        self.assertNotIn("real_repo_changed", state.read_jsonl(state.path("runs.jsonl"))[-1])

    def test_disabled_state_error_does_not_break_the_review(self):
        real = state.locked

        def locked(name="global"):
            if name == "disabled":
                raise OSError("disk")
            return real(name)
        with mock.patch.object(state, "locked", locked):
            out = self.stop(f"echo x > {self.repo}/evil.txt", BLOCK)
        self.assertEqual(out["decision"], "block")


class Hardening(unittest.TestCase):
    def test_worktree_add_runs_no_hooks(self):
        repo = make_repo()
        hook = pathlib.Path(repo, ".git", "hooks", "post-checkout")
        marker = os.path.join(tempfile.mkdtemp(), "ran")
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        with sandbox.checkout(repo, changes.snapshot(repo)) as path:
            self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.exists(marker))

    def test_round_two_prompt_never_leaks_real_path(self):
        repo = make_repo()
        write_user_config(use_toml(["copilot"]))
        seen = {}

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            seen.update(jobs=jobs, cwd=cwd)
            return {r: ({"reviewer": r, "ok": True, "duration": 1}, APPROVE) for r in jobs}
        pathlib.Path(repo, "a.py").write_text("y = 2\n" * 30)
        ev = types.SimpleNamespace(agent="claude", session_id="s1", cwd=repo, last_message=f"I fixed {repo}/a.py")
        prior = [{"severity": "major", "file": f"{repo}/a.py", "line": 1, "claim": f"bad {repo}/a.py", "evidence": "",
                  "suggested_fix": "", "reviewers": ["codex"]}]
        base = changes.refs(repo)
        s = {"prompt": f"edit {repo}/a.py", "repo": repo, "touched": [], "prior": prior, "refs": base}
        artifact = {"path": "a.py", "status": "modified", "lines_changed": 30, "diff": f"+see {repo}/a.py"}
        with mock.patch.object(harnesses, "detected", return_value=["copilot"]), \
                mock.patch.object(core, "headroom_map", return_value={}), mock.patch.object(runner, "run_all", run_all):
            core.review(ev, config.load(), s, [artifact], 2, "turn", limits.HELD)
        self.assertTrue(seen["jobs"])
        for r, prompt in seen["jobs"].items():
            self.assertNotIn(repo, prompt)
            self.assertIn(seen["cwd"][r] + "/a.py", prompt)
        write_user_config("")

    def test_idle_worktree_is_cleared_but_a_fresh_one_is_kept(self):
        repo = make_repo()
        old, fresh = (os.path.join(tempfile.mkdtemp(), f"tagteam-review-{n}") for n in ("old", "fresh"))
        for d in (old, fresh):
            sh(repo, "git", "worktree", "add", "--detach", d)
        os.utime(old, (time.time() - 3 * 3600,) * 2)
        with sandbox.checkout(repo, changes.snapshot(repo)):
            pass
        names = " ".join(worktrees(repo))
        self.assertNotIn("tagteam-review-old", names)
        self.assertIn("tagteam-review-fresh", names)
        self.assertTrue(os.path.isdir(fresh))

    def test_stale_worktree_from_a_killed_review_is_cleared(self):
        repo = make_repo()
        stale = os.path.join(tempfile.mkdtemp(), "tagteam-review-dead")
        sh(repo, "git", "worktree", "add", "--detach", stale)
        other = os.path.join(tempfile.mkdtemp(), "someone-elses")
        sh(repo, "git", "worktree", "add", "--detach", other)
        shutil.rmtree(stale)
        shutil.rmtree(other)
        with sandbox.checkout(repo, changes.snapshot(repo)):
            self.assertEqual(len(worktrees(repo)), 3)            # main + other tool's entry + ours; stale gone
        self.assertEqual(len(worktrees(repo)), 2)
        self.assertTrue(any("someone-elses" in w for w in worktrees(repo)))

    def test_temp_dir_inside_repo_falls_back_to_cwd(self):
        repo = make_repo()
        tree = changes.snapshot(repo)
        with mock.patch.object(tempfile, "tempdir", repo), sandbox.checkout(repo, tree, cwd=repo) as path:
            self.assertEqual(path, repo)
        self.assertEqual(os.listdir(repo).count("a.py"), 1)
        self.assertEqual([n for n in os.listdir(repo) if n.startswith("tagteam-review-")], [])

    def test_tilde_paths_for_out_of_repo_artifacts(self):
        home = tempfile.mkdtemp()
        p = os.path.join(home, ".claude", "plans", "p.md")
        task = [{"kind": "plan", "lens": "design",
                 "artifacts": [{"path": p, "status": "modified", "lines_changed": 3, "diff": f"(full content of {p})\nx"}]}]
        with mock.patch.dict(os.environ, {"HOME": home}):
            prompt = runner.build_prompt("codex", "claude", "/sb", "", task, 10000)
        self.assertNotIn(home, prompt)
        self.assertIn("### ~/.claude/plans/p.md", prompt)


class OutsideEdit(unittest.TestCase):
    def test_user_edit_of_touched_outside_file_in_sandboxed_review_disables_nobody(self):
        repo = make_repo()
        plan = os.path.join(os.path.realpath(tempfile.mkdtemp()), "plan.md")
        pathlib.Path(plan).write_text("v1\n")
        write_user_config(use_toml(["copilot"]))
        ev = types.SimpleNamespace(agent="claude", session_id="s1", cwd=repo, last_message="")
        s = {"prompt": "", "repo": repo, "touched": [plan], "prior": [], "refs": changes.refs(repo)}
        artifact = {"path": plan, "status": "modified", "lines_changed": 30, "diff": "x"}
        with mock.patch.object(harnesses, "detected", return_value=["copilot"]), \
                mock.patch.object(core, "headroom_map", return_value={}), \
                mock.patch.object(harnesses, "get", fake_harness(f"echo v2 > {plan}")):
            action, msg, _, _ = core.review(ev, config.load(), s, [artifact], 1, "turn", limits.HELD)
        self.assertEqual(limits.disabled(), {})
        self.assertIn("⚠ your repo changed during review (see tagteam.log)", msg)
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["real_repo_changed"], [plan])
        write_user_config("")


class NonGit(unittest.TestCase):
    def test_non_git_dir_stops_the_review_when_the_files_change(self):
        d = os.path.realpath(tempfile.mkdtemp())
        notes = os.path.join(d, "notes.py")
        pathlib.Path(notes).write_text("x = 1\n")
        write_user_config(use_toml(["copilot"]))
        ev = types.SimpleNamespace(agent="claude", session_id="s1", last_message="")
        s = {"prompt": "", "repo": None, "touched": [notes], "prior": [], "refs": {}}
        artifact = {"path": notes, "status": "modified", "lines_changed": 30, "diff": "x"}
        with mock.patch.object(harnesses, "detected", return_value=["copilot"]), \
                mock.patch.object(core, "headroom_map", return_value={}), \
                mock.patch.object(harnesses, "get", fake_harness(f"echo y >> {notes}")):
            ev.cwd = d
            action, msg, run_dir, open_issues = core.review(ev, config.load(), s, [artifact], 1, "turn", limits.HELD)
        self.assertEqual((action, run_dir, open_issues, limits.disabled()), ("allow", None, None, {}))
        self.assertIn(core.STOPPED_IN_PLACE, msg)
        write_user_config("")


class BackgroundStop(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig(small_blocks=False)
        write_user_config(use_toml(["copilot"]))

    def tearDown(self):
        self.unrig()

    def test_stopped_background_review_leaves_the_change_for_the_next_turn(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        pathlib.Path(self.repo, "bg.py").write_text("a = 1\n" * 20)
        with mock.patch.object(harnesses, "get", fake_harness("git config tagteam.x 1")):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["reason"], core.STOPPED)
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="next"))
        pathlib.Path(self.repo, "other.py").write_text("b = 2\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertIn("bg.py", next(iter(self.calls[-1].values())))


    def test_background_stop_reported_after_the_next_turn_began_is_still_reviewed(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        pathlib.Path(self.repo, "bg.py").write_text("a = 1\n" * 20)
        self.run_detached = False
        self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="next"))   # before the review reports
        with mock.patch.object(harnesses, "get", fake_harness("git config tagteam.x 1")):
            core.background(self.detached[-1])
        self.run_detached = True
        pathlib.Path(self.repo, "other.py").write_text("b = 2\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertIn("bg.py", next(iter(self.calls[-1].values())))


    def test_background_stop_after_a_timed_out_stop_is_still_reviewed(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="turn A"))
        pathlib.Path(self.repo, "bg.py").write_text("a = 1\n" * 20)
        self.run_detached = False
        self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="turn B"))
        pathlib.Path(self.repo, "b.py").write_text("b = 1\n" * 20)
        core._still_running(HookEvent("claude", "turn-end", "sess1", self.repo), config.load())   # B's Stop waited out
        with mock.patch.object(harnesses, "get", fake_harness("git config tagteam.x 1")):
            core.background(self.detached[-1])                    # A's review stops only now
        self.run_detached = True
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="turn C"))
        pathlib.Path(self.repo, "c.py").write_text("c = 1\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        prompt = next(iter(self.calls[-1].values()))
        self.assertEqual([f in prompt for f in ("bg.py", "b.py", "c.py")], [True, True, True])


class Overlay(unittest.TestCase):
    def test_overlay_adds_files_to_head_and_touches_nothing_else(self):
        repo = make_repo()
        pathlib.Path(repo, "a.py").write_text("dirty\n")
        g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout
        before = [g("status", "--porcelain"), g("ls-files", "-s"), g("for-each-ref"), g("rev-parse", "HEAD")]
        tree = sandbox.overlay(repo, {".mcp.json": "{}", "deep/x.json": "[1]"})
        self.assertEqual([g("status", "--porcelain"), g("ls-files", "-s"), g("for-each-ref"), g("rev-parse", "HEAD")],
                         before)
        self.assertEqual(sorted(g("ls-tree", "-r", "--name-only", tree).split()), [".mcp.json", "a.py", "deep/x.json"])
        self.assertEqual(g("show", f"{tree}:a.py"), "x = 1\n")
        self.assertEqual(g("show", f"{tree}:deep/x.json"), "[1]")


class Router(unittest.TestCase):
    def test_candidates_skip_disabled(self):
        write_user_config(use_toml(["codex", "copilot"]))
        with mock.patch.object(harnesses, "detected", return_value=["codex", "copilot"]):
            self.assertEqual(router.candidates("claude", config.load()), ["codex", "copilot"])
            limits.disable("codex", "wrote to the real tree")
            self.assertEqual(router.candidates("claude", config.load()), ["copilot"])
            self.assertEqual(limits.disabled()["codex"]["reason"], "wrote to the real tree")
        write_user_config("")


if __name__ == "__main__":
    unittest.main()
