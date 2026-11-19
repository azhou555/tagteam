import time
import unittest

from tests.helpers import CFG  # noqa: F401  # isort: skip
from tagteam import health, state  # noqa: E402

NEEDS = {"reviewers": {"copilot": {"status": "needs_you", "reason": "not logged in → copilot login"},
                        "agy": {"status": "ok", "reason": ""}}}


def hist(reviewer, oks):
    now = time.time()
    for i, ok in enumerate(oks):
        state.append_jsonl(state.path("history.jsonl"), {"ts": now - 100 + i, "reviewer": reviewer, "ok": ok})


class Health(unittest.TestCase):
    def test_notice_once_per_day(self):
        self.assertEqual(health.notice(), "")
        health.save(NEEDS)
        self.assertEqual(health.needs_you(), [("copilot", "copilot: not logged in → copilot login")])
        t = time.time()
        self.assertEqual(health.notice(t), " · ⚠ copilot: not logged in → copilot login")
        self.assertEqual(health.notice(t + 10), "")
        self.assertEqual(health.notice(t + 86401), " · ⚠ copilot: not logged in → copilot login")

    def test_authors_need_you_in_their_own_namespace(self):
        health.save({"authors": {"claude": {"status": "needs_you", "reason": "stale hooks → tagteam install"}}})
        self.assertEqual(health.needs_you(), [("author:claude", "claude: stale hooks → tagteam install")])

    def test_no_files_means_healthy(self):
        self.assertFalse(health.is_demoted("agy"))
        self.assertEqual(health.load(), {})

    def test_demoted_after_three_of_four_failures(self):
        for oks, want in [([True, False, False, False], True), ([False, False, False, True], False),
                          ([False, True, False, False], True), ([False, False, False], False)]:
            state.path("history.jsonl").unlink(missing_ok=True)
            hist("agy", oks)
            self.assertEqual(health.is_demoted("agy"), want, oks)

    def test_other_reviewers_rows_ignored(self):
        hist("codex", [False] * 4)
        self.assertFalse(health.is_demoted("agy"))

    def test_probe_success_clears_demotion(self):
        hist("agy", [False] * 4)
        self.assertTrue(health.is_demoted("agy"))
        health.note_probe("agy", True, "probe")
        self.assertFalse(health.is_demoted("agy"))

    def test_unreadable_health_is_empty(self):
        state.path("health.json").write_text("{")
        self.assertEqual(health.load(), {})

    def test_save_load_roundtrip(self):
        health.save({"last_tick": 1.0, "reviewers": {}})
        self.assertEqual(health.load()["last_tick"], 1.0)

    def test_events_roundtrip(self):
        health.event("agy", "demoted", "3 of 4 failed")
        health.event("agy", "restored", "probe ok")
        self.assertEqual([(e["action"], e["why"]) for e in health.events(5)],
                         [("demoted", "3 of 4 failed"), ("restored", "probe ok")])
