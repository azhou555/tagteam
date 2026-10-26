import dataclasses
import os
import pathlib
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from tests.helpers import HookRig, finding, make_repo, state, use_toml, write_user_config

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
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            os.environ.pop(k, None)
        with mock.patch.dict(os.environ, env):
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

    def test_reviewer_writes_land_in_sandbox_only(self):
        self.stop("echo x > pwned.txt")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "pwned.txt")))
        self.assertEqual(limits.disabled(), {})
        self.assertEqual(len(worktrees(self.repo)), 1)
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "status", "--porcelain"], capture_output=True,
                                        text=True).stdout.split(), ["??", "feature.py"])

    def test_prompt_never_contains_real_checkout_path(self):
        seen = {}

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            seen.update(jobs=jobs, cwd=cwd, exists=os.path.isdir(cwd))
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

    def test_real_tree_write_disables_reviewer(self):
        out = self.stop(f"echo x > {self.repo}/evil.txt", BLOCK)
        self.assertEqual(out["decision"], "block")                       # the round's verdict is still delivered
        self.assertIn("copilot", limits.disabled())
        runs = state.read_jsonl(state.path("runs.jsonl"))
        self.assertTrue(runs[-1]["wrote_real_tree"])
        self.calls.clear()
        pathlib.Path(self.repo, "more.py").write_text("b = 2\n" * 30)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            out = self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.assertEqual(self.calls, [])                                 # disabled reviewer is skipped
        self.assertIn("no listed reviewer is available", state.read_jsonl(state.path("runs.jsonl"))[-1]["reason"])
        limits.clear_disabled()
        self.assertEqual(limits.disabled(), {})
        self.assertEqual(router.candidates("claude", config.load()), ["copilot"])

    def test_disabled_state_error_does_not_break_the_review(self):
        real = state.locked

        def locked(name="global"):
            if name == "disabled":
                raise OSError("disk")
            return real(name)
        with mock.patch.object(state, "locked", locked):
            out = self.stop(f"echo x > {self.repo}/evil.txt", BLOCK)
        self.assertEqual(out["decision"], "block")


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
