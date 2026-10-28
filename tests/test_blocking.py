import unittest

from tests.helpers import CFG, finding

from tagteam import verdict


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


if __name__ == "__main__":
    unittest.main()
