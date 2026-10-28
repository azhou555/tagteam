import pathlib
import threading
import time
import unittest
from unittest import mock

from tests.helpers import CFG, HookRig, finding, state, use_toml, write_user_config

from tagteam import limits, live, runner, verdict

BLOCK_SMALL = "[review]\nblocking = { small = true }\n"


def res(*findings):
    return {"copilot": {"findings": list(findings)}}


class Gates(unittest.TestCase):
    CARRIED = [{**finding(claim="cache key ignores the locale"), "reviewers": ["copilot"], "gate": "advise"}]

    def gates(self, findings, mode="block", carried=()):
        action, issues, advisory = verdict.decide(res(*findings), CFG, 1, mode, carried)
        return action, [(f["severity"], f["gate"]) for f in issues], len(advisory)

    def test_a_non_blocking_review_suggests_and_a_blocker_still_blocks(self):
        self.assertEqual(self.gates([finding(), finding("nit", line=90)], "advise"), ("allow", [("major", "advise")], 1))
        self.assertEqual(self.gates([finding("blocker")], "advise"), ("block", [("blocker", "block")], 0))

    def test_a_rechecked_suggestion_stays_one_unless_it_is_now_a_blocker(self):
        again = finding(claim="Cache key ignores the locale", line=11)
        self.assertEqual(self.gates([again], "block", self.CARRIED), ("allow", [("major", "advise")], 0))
        self.assertEqual(self.gates([{**again, "severity": "blocker"}], "block", self.CARRIED),
                         ("block", [("blocker", "block")], 0))

    def test_a_new_issue_in_a_blocking_review_blocks_beside_a_carried_suggestion(self):
        new = finding(claim="writes the file before validating it", line=40)
        again = finding(claim="cache key ignores the locale")
        self.assertEqual(self.gates([again, new], "block", self.CARRIED),
                         ("block", [("major", "advise"), ("major", "block")], 0))

    def test_the_message_says_which_kind(self):
        _, issues, _ = verdict.decide(res(finding(claim="cache key ignores the locale"),
                                          finding(claim="writes the file before validating it", line=40)),
                                      CFG, 1, "block", self.CARRIED)
        msg = verdict.message({"copilot": [{"lens": "correctness"}]}, issues, [], 1, 2, [])
        self.assertEqual(msg.splitlines(), [
            "[tagteam] Cross-review by copilot (correctness), round 1/2.",
            "Blocking: fix each item below. If you disagree with one, say why in your reply; reviewers see your "
            "reply next round.", "",
            "1. [major] a.py:40: writes the file before validating it  (copilot)", "   Evidence: e", "   Fix: f", "",
            "Suggestions (not blocking; fix when convenient):",
            "1. [major] a.py:10: cache key ignores the locale  (copilot)", "   Evidence: e", "   Fix: f"])


class Flow(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig(use_toml(["copilot"]) + BLOCK_SMALL)

    def tearDown(self):
        self.unrig()

    def start(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))

    def stop(self, results=None):
        with mock.patch.object(runner, "run_all", self.fake_run_all(results or {})):
            return self.run_hook("claude", "x", self.claude("Stop", last_assistant_message="done"))

    def write(self, name, n=20):
        pathlib.Path(self.repo, name).write_text("a = 1\n" * n)

    def seg(self):
        return live.segment("claude", "sess1")

    def hold_lock(self, secs):
        """Another review of this session, running for `secs`."""
        held = threading.Event()

        def run():
            with limits.session_lock("claude", "sess1"):
                held.set()
                time.sleep(secs)
        threading.Thread(target=run, daemon=True).start()
        held.wait()

    def test_the_next_stop_waits_for_a_running_review(self):
        self.start()
        self.write("feature.py")
        t0 = time.monotonic()
        self.hold_lock(0.6)
        self.assertEqual(self.stop()["systemMessage"].split(" · ")[0], "[tagteam] ✓ Review passed: no issues found")
        self.assertGreaterEqual(time.monotonic() - t0, 0.6)
        self.assertEqual(len(self.calls), 1)

    def test_at_the_deadline_it_is_not_reviewed_and_the_next_review_covers_both_turns(self):
        self.start()
        self.write("one.py")
        write_user_config(use_toml(["copilot"]) + BLOCK_SMALL + "deadline_s = 1\n")
        self.hold_lock(3)
        out = self.stop()
        self.assertEqual((out["systemMessage"], self.calls), ("[tagteam] ⚠ Not reviewed: earlier review still running", []))
        self.assertIn("⚠ Not reviewed: earlier review still running", self.seg())
        time.sleep(2.2)
        write_user_config(use_toml(["copilot"]) + BLOCK_SMALL)
        self.start()
        self.write("two.py")
        self.stop()
        prompt = next(iter(self.calls[-1].values()))
        self.assertEqual(("one.py" in prompt, "two.py" in prompt), (True, True))
        self.start()
        self.write("three.py")
        self.stop()
        prompt = next(iter(self.calls[-1].values()))
        self.assertEqual(("two.py" in prompt, "three.py" in prompt), (False, True))   # pending is used once


if __name__ == "__main__":
    unittest.main()
