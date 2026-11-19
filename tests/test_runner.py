import json
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import CFG  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import quota, runner  # noqa: E402
from tagteam.harnesses import get  # noqa: E402

DENIED_OUT = json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "read_file",
                                                                                  "display_name": "ReadFile"}]})
GOOD = json.dumps({"status": "SUCCESS", "response": json.dumps({"verdict": "approve", "summary": "", "findings": []})})


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class AgyWorktreeRead(unittest.TestCase):
    def test_review_worktree_may_read_its_own_gitdir_only(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        git(repo, "init", "-q")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i")
        wt = repo.parent / (repo.name + "-wt")
        git(repo, "worktree", "add", "-q", "--detach", str(wt))
        argv = get("agy").reviewer.argv("P", {}, "/w", 100, str(wt))[0]
        allow = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
        gitdir = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], cwd=wt, capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(allow, [f"read_file({gitdir})"])
        self.assertIn("/.git/worktrees/", gitdir)

    def test_no_rule_outside_a_worktree(self):
        repo = tempfile.mkdtemp()
        git(repo, "init", "-q")                       # a real .git directory is already inside the workspace
        self.assertNotIn("--settings", get("agy").reviewer.argv("P", {}, "/w", 100, repo)[0])
        self.assertNotIn("--settings", get("agy").reviewer.argv("P", {}, "/w", 100, tempfile.mkdtemp())[0])


class RunOne(unittest.TestCase):
    def run_one(self, agent, outputs):
        spawn = mock.Mock(side_effect=outputs)
        with mock.patch.object(runner, "_spawn", spawn):
            rec, result = runner.run_one(agent, CFG, "PROMPT", tempfile.mkdtemp(), 240, tempfile.mkdtemp())
        return rec, result, spawn

    def test_denied_read_retry_names_the_read(self):
        rec, result, spawn = self.run_one("agy", [(DENIED_OUT, "", 0, False), (GOOD, "", 0, False)])
        self.assertIsNotNone(result)
        retry_prompt = spawn.call_args_list[1].args[0][2]
        self.assertIn("was denied read_file", retry_prompt)
        self.assertNotIn("ran a shell command", retry_prompt)

    def test_error_is_stderr_reason_not_stdout_envelope(self):
        why = 'jetski: no output produced — a tool required the "read_file" permission'
        rec, _, _ = self.run_one("agy", [(DENIED_OUT, why, 0, False)] * 2)
        self.assertEqual(rec["error"], why)

    def test_error_without_stderr_is_last_error_line(self):
        out = '{"type":"step_start"}\n{"type":"error","error":{"message":"boom"}}\n{"type":"step_start"}\n'
        rec, _, _ = self.run_one("opencode", [(out, "", 1, False)])
        self.assertEqual(rec["error"], '{"type":"error","error":{"message":"boom"}}')

    def test_timeout_says_so(self):
        rec, _, _ = self.run_one("opencode", [('{"type":"step_start"}', "", -9, True)])
        self.assertEqual(rec["error"], "timed out")

    def test_quota_cooldown_honors_retry_after(self):
        body = ('{"type":"error","error":{"type":"provider.free-limit","message":"Free usage limit reached",'
                '"status":429},"retryAfterMs":18000000}')     # 5 hours
        t = time.time()
        rec, _, _ = self.run_one("opencode", [(body, "", 1, False)])
        self.assertTrue(rec["quota_error"])
        self.assertAlmostEqual(quota.cooldowns()["opencode"] - t, 5 * 3600, delta=60)

    def test_quota_words_in_files_the_reviewer_read_are_not_a_quota_error(self):
        read = json.dumps({"type": "tool", "part": {"state": {"output": (
            '98: # Trust X-Forwarded-For for the client IP (rate limiting). Free usage limit reached\n'
            '99: "retryAfterMs":999999999999')}}})
        failed_read = json.dumps({"type": "tool", "part": {"state": {"status": "error", "error": "cut off", "output": (
            "# client IP (rate limiting). Free usage limit reached")}}})
        for out in [(failed_read, "", -9, True), (read, "", 1, False)]:   # the 2026-11-05 false pause was a timeout
            rec, _, _ = self.run_one("opencode", [out])
            self.assertNotIn("quota_error", rec)
            until = (quota.cooldowns() or {}).get("opencode", 0)    # two failures pause it the usual 15 minutes
            self.assertLess(until - time.time(), CFG["limits"]["cooldown_min"] * 60 + 60)

    def test_quota_in_stderr_counts_even_on_timeout(self):
        rec, _, _ = self.run_one("opencode", [('{"type":"step_start"}', "Error: 429 Too Many Requests", -9, True)])
        self.assertTrue(rec.get("quota_error"))

    def test_quota_cooldown_default_and_cap(self):
        default = CFG["limits"]["cooldown_min"] * 4
        self.assertEqual(runner._quota_minutes("rate limit", default), default)
        self.assertEqual(runner._quota_minutes('"retryAfterMs":1000', default), default)        # never shorter
        self.assertEqual(runner._quota_minutes('\\"retryAfterMs\\":999999999999', default), 24 * 60)


if __name__ == "__main__":
    unittest.main()
