import json
import pathlib
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import CFG, fx
from tagteam import harnesses, live, runner, state


def run(usage=None, **kw):
    r = {"ts": time.time(), "run_id": "r", "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
         "reviewers": ["agy"], "action": "allow", "blocking": 0, "advisory": 0, "failed": [], "duration": 1.0}
    if usage is not None:
        r["usage"] = usage
    return {**r, **kw}


class Spend(unittest.TestCase):
    def test_usage_parsers(self):
        for agent in ("claude", "codex", "copilot", "agy"):
            f = fx(f"usage-{agent}")
            with tempfile.TemporaryDirectory() as wd:
                for name, data in f["files"].items():
                    (pathlib.Path(wd) / name).write_text(json.dumps(data))
                got = harnesses.get(agent).reviewer.usage(f["stdout"], wd)
            self.assertEqual(got, f["expected"], agent)

    def test_unparseable_output_gives_empty_usage(self):
        for agent in ("claude", "codex", "copilot", "agy"):
            for junk in ("", "not json", "[1, 2]", '{"usage": 5, "total_cost_usd": "x"}'):
                with tempfile.TemporaryDirectory() as wd:
                    self.assertEqual(harnesses.get(agent).reviewer.usage(junk, wd), {}, (agent, junk))

    def test_run_one_records_usage_and_keeps_premium_cost(self):
        for agent, out in (("claude", fx("usage-claude")["stdout"]), ("agy", fx("usage-agy")["stdout"])):
            with tempfile.TemporaryDirectory() as rd, mock.patch.object(runner, "_spawn", return_value=(out, "", 0, False)):
                meta, res = runner.run_one(agent, CFG, "p", rd, 60, rd)
            self.assertIsNotNone(res)
            self.assertEqual(meta["usage"], harnesses.get(agent).reviewer.usage(out, rd))
        def spawn(argv, stdin, cwd, timeout):               # copilot writes usage.json where argv says
            pathlib.Path(argv[argv.index("--usage-output-file") + 1]).write_text(
                json.dumps(fx("usage-copilot")["files"]["usage.json"]))
            return '{"findings": []}', "", 0, False
        with tempfile.TemporaryDirectory() as rd, mock.patch.object(runner, "_spawn", side_effect=spawn):
            meta, _ = runner.run_one("copilot", CFG, "p", rd, 60, rd)
        self.assertEqual((meta["cost"], meta["usage"]), (1, {"premium_requests": 1}))

    def test_week_spend_sums_known_and_counts_unknown(self):
        now = time.time()
        for r in (run({"claude": {"cost_usd": 1.5}}), run({"agy": {"input_tokens": 9}, "claude": {"cost_usd": 0.34}}),
                  run(), run({"copilot": {"premium_requests": 1}}),
                  run(ts=now - 8 * 86400, usage={"claude": {"cost_usd": 100.0}})):
            live.record_run(r)
        reviews, cost, unreported = live.week_spend(now + 1)
        self.assertEqual((reviews, round(cost, 2), unreported), (4, 1.84, 2))

    def test_watch_and_swiftbar_show_week_line(self):
        self.assertIn("This week: 0 reviews · cost not reported", live.render_watch())
        for _ in range(11):
            live.record_run(run())
        live.record_run(run({"copilot": {"premium_requests": 1}}))
        self.assertIn("This week: 12 reviews · cost not reported · 1 premium request", live.render_watch())
        live.record_run(run({"claude": {"cost_usd": 1.84}, "copilot": {"premium_requests": 5}}))
        line = "This week: 13 reviews · ~$1.84 reported · 12 didn't report cost · 6 premium requests"
        self.assertIn(line, live.render_watch())
        self.assertIn(line, live.render_swiftbar())
        self.assertIn("This week: 13 reviews · ~$1.84 reported", live.render_swiftbar())

    def test_line_formats(self):
        self.assertEqual(live.spend_line(14, 1.84, 3, 0), "This week: 14 reviews · ~$1.84 reported · 3 didn't report cost")
        self.assertEqual(live.spend_line(2, 0.5, 0, 0), "This week: 2 reviews · ~$0.50 reported")
        self.assertEqual(live.spend_line(14, 0.0, 14, 0), "This week: 14 reviews · cost not reported")
        self.assertEqual(live.spend_line(1, 0.0, 1, 6), "This week: 1 review · cost not reported · 6 premium requests")
