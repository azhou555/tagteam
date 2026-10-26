import dataclasses
import os
import shutil
import pathlib
import subprocess
import tempfile
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
    rev = Reviewer(argv=lambda prompt, options, workdir, deadline: (["sh", "-c", script], None, None),
                   parse=lambda out, f: result, usage=no_usage)
    real = harnesses.get
    return lambda name: dataclasses.replace(real(name), reviewer=rev) if name == "copilot" else real(name)


def worktrees(repo):
    out = subprocess.run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line.startswith("worktree ")]


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

    def test_worktree_failure_falls_back_to_cwd(self):
        repo = make_repo()
        with sandbox.checkout(repo, "0" * 40, cwd=repo) as path:
            self.assertEqual(path, repo)

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

    def test_reviewer_writes_land_in_sandbox_only_and_disable_that_reviewer(self):
        self.stop("echo x > pwned.txt")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pwned.txt")))
        self.assertEqual(list(limits.disabled()), ["copilot"])
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

    def test_git_config_change_disables_every_reviewer(self):
        self.stop("git config core.hooksPath /evil")
        self.assertEqual(list(limits.disabled()), ["copilot"])
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "config", "core.hooksPath"], capture_output=True,
                                        text=True).stdout.strip(), "/evil")

    def test_hook_install_in_shared_git_dir_is_detected(self):
        self.stop("echo x > $(git rev-parse --git-common-dir)/hooks/pre-commit")
        self.assertEqual(list(limits.disabled()), ["copilot"])

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


class NonGit(unittest.TestCase):
    def test_non_git_dir_falls_back_to_flags_and_postcondition(self):
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
            core.review(ev, config.load(), s, [artifact], 1, "turn", limits.HELD)
        self.assertIn("copilot", limits.disabled())
        write_user_config("")


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
