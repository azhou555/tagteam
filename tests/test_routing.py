import copy
import pathlib
import subprocess
import unittest
from unittest import mock

from tests.helpers import (ALL_AGENTS, CFG, FULL, HookRig, art, clear_user_config, state, use_toml,  # isort: skip
                           write_user_config)
from tagteam import core, harnesses, live, quota, router, runner  # noqa: E402

NOTICE_NONE = "[tagteam] ⚠ Not reviewed: no reviewers configured; run tagteam install"
NOTICE_UNAVAILABLE = "[tagteam] ⚠ Not reviewed: no listed reviewer is available"


def cfg_with(use, mode="ordered", **per_change):
    cfg = copy.deepcopy(CFG)
    cfg["routing"]["mode"] = mode
    cfg["reviewers"]["use"] = use
    cfg["review"]["per_change"].update(per_change)
    return cfg


class Routing(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(harnesses, "detected", return_value=list(ALL_AGENTS))
        p.start()
        self.addCleanup(p.stop)

    def test_ordered_picks_in_list_order(self):
        cfg = cfg_with(["copilot", "codex", "agy"], large=2)
        groups = {"code": [art("a.py", 200)]}
        plan, reason = router.route("claude", groups, cfg, FULL, {})
        self.assertEqual((list(plan), reason), (["copilot", "codex"], None))
        with mock.patch.object(quota, "cooling", side_effect=lambda a, now=None: a == "copilot"):
            plan, _ = router.route("claude", groups, cfg, FULL, {})
        self.assertEqual(list(plan), ["codex", "agy"])

    def test_tier_picks_one_bucket_per_change(self):
        rv = CFG["review"]
        cases = [("code", [art("a.py", 149)], "small"), ("code", [art("a.py", 100), art("b.py", 50)], "large"),
                 ("code", [art("auth.py", 3, risk="high")], "high_risk"), ("design", [art("d.md", 3)], "docs"),
                 ("plan", [art("p.md", 500)], "docs"), ("plan", [art("p.md", risk="high")], "high_risk")]
        self.assertEqual([router.tier(k, a, rv) for k, a, _ in cases], [t for _, _, t in cases])
        self.assertEqual({t: rv["blocking"][t] for t in ("small", "large", "high_risk", "docs")},
                         {"small": False, "large": True, "high_risk": True, "docs": True})
        self.assertEqual(router.needed("code", [art("a.py", 10)], cfg_with([], small=3)["review"]), 3)

    def test_duplicate_use_entries_collapse_keeping_order(self):
        self.assertEqual(router.candidates("claude", cfg_with(["codex", "copilot", "codex"])), ["codex", "copilot"])
        plan, _ = router.route("claude", {"code": [art("a.py", 200)]}, cfg_with(["codex", "codex", "agy"], large=2),
                               FULL, {})
        self.assertEqual(list(plan), ["codex", "agy"])

    def test_ordered_small_change_takes_first_only_and_skips_author(self):
        cfg = cfg_with(["claude", "codex", "agy"])
        plan, _ = router.route("claude", {"code": [art("a.py", 10)]}, cfg, FULL, {})
        self.assertEqual(list(plan), ["codex"])

    def test_unlisted_never_planned_either_mode(self):
        groups = {"code": [art("auth.py", 500, risk="high")], "design": [art("d.md", kind="design")],
                  "plan": [art("p.md", kind="plan")]}
        for mode in ("ordered", "quota"):
            for author in ALL_AGENTS:
                cfg = cfg_with(["codex"], mode, small=4, large=4, high_risk=4, docs=4)
                plan, _ = router.route(author, groups, cfg, FULL, {})
                self.assertLessEqual(set(plan), {"codex"}, (mode, author, plan))
        plan, _ = router.route("claude", groups, cfg_with(["codex"], "quota", high_risk=4), FULL, {})
        self.assertEqual(list(plan), ["codex"])                    # control: the listed one is planned

    def test_only_author_or_missing_listed(self):
        for use in (["claude"], ["kiro"], ["claude", "kiro"]):
            for mode in ("ordered", "quota"):
                self.assertEqual(router.route("claude", {"code": [art("a.py")]}, cfg_with(use, mode), FULL, {}),
                                 ({}, "no listed reviewer is available"), (use, mode))

    def test_no_list_is_no_reviewers_configured(self):
        for mode in ("ordered", "quota"):
            for use in ([], None):
                cfg = cfg_with([], mode)
                if use is None:
                    del cfg["reviewers"]["use"]
                self.assertEqual(router.route("claude", {"code": [art("a.py")]}, cfg, FULL, {}),
                                 ({}, "no reviewers configured"))

    def test_undetected_listed_agent_is_unavailable(self):
        with mock.patch.object(harnesses, "detected", return_value=["claude"]):
            self.assertEqual(router.route("claude", {"code": [art("a.py")]}, cfg_with(["codex"]), FULL, {}),
                             ({}, "no listed reviewer is available"))



class Probes(HookRig, unittest.TestCase):
    """Quota probes (Copilot's is `gh api`) run only in quota mode, and only for listed agents."""

    def setUp(self):
        self.rig()
        self.rigs.pop(0).stop()                                 # the real headroom_map, not the rig's stub
        self.argv = []
        real = subprocess.run

        def run(argv, *a, **kw):
            self.argv.append(list(argv))
            if argv[0] == "gh":                                 # never the real gh
                return subprocess.CompletedProcess(argv, 1, "", "")
            return real(argv, *a, **kw)
        p = mock.patch("subprocess.run", side_effect=run)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.unrig()

    def review(self, user_config):
        write_user_config(user_config)
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        self.n = getattr(self, "n", 0) + 1
        pathlib.Path(self.repo, f"feature{self.n}.py").write_text("a = 1\n" * 20)
        self.argv.clear()
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop"))
        return [a for a in self.argv if a[0] != "git"]

    def test_ordered_mode_runs_no_probe(self):
        self.assertEqual(self.review(use_toml(["copilot", "agy"])), [])
        self.assertEqual(len(self.calls), 1)                     # the review itself still ran

    def test_quota_mode_probes_only_listed_agents(self):
        self.assertEqual(self.review(use_toml(["copilot"]) + '[routing]\nmode = "quota"\n'),
                         [["gh", "api", "/copilot_internal/user"]])
        with mock.patch.object(quota, "headroom", wraps=quota.headroom) as probe:
            self.assertEqual(self.review(use_toml(["agy", "kiro"]) + '[routing]\nmode = "quota"\n'), [])
        self.assertEqual([c.args[0] for c in probe.call_args_list], ["agy"])
        self.assertEqual(core.headroom_map(cfg_with(["copilot"])), {})

class Consent(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig()

    def tearDown(self):
        self.unrig()

    def stop_with_change(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        self.n = getattr(self, "n", 0) + 1
        pathlib.Path(self.repo, f"feature{self.n}.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            return self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))

    def assert_no_spawn_notice(self, out, notice):
        self.assertEqual(self.calls, [])
        self.assertEqual(out["systemMessage"], notice)
        last = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual(last["reviewers"], [])
        self.assertEqual(live.outcome(last)[0], "unreviewed")
        self.assertEqual(live.summary(last), notice.removeprefix("[tagteam] "))

    def test_no_list_no_spawn(self):
        cases = {"missing user config": None, "empty use": use_toml([]), "repo only": None}
        for name, user in cases.items():
            with self.subTest(name):
                self.calls.clear()
                clear_user_config()
                if user is not None:
                    write_user_config(user)
                repo_file = pathlib.Path(self.repo, ".tagteam.toml")
                repo_file.unlink(missing_ok=True)
                if name == "repo only":
                    repo_file.write_text('[reviewers]\nuse = ["copilot"]\n[routing]\nmode = "ordered"\n')
                self.assert_no_spawn_notice(self.stop_with_change(), NOTICE_NONE)

    def test_legacy_config_without_consent_spawns_nothing(self):
        from tagteam import config
        write_user_config('[agents.copilot]\nenabled = true\n[agents.agy]\nenabled = true\n'
                          '[routing]\nsmall = 1\nlarge = 2\n')
        self.assertNotIn("use", config.load()["reviewers"])
        self.assert_no_spawn_notice(self.stop_with_change(), NOTICE_NONE)

    def test_listed_but_unavailable_notice(self):
        write_user_config(use_toml(["claude"]))                    # only the author
        self.assert_no_spawn_notice(self.stop_with_change(), NOTICE_UNAVAILABLE)
        self.calls.clear()
        write_user_config(use_toml(["kiro"]))                      # not an installed agent
        self.assert_no_spawn_notice(self.stop_with_change(), NOTICE_UNAVAILABLE)

    def test_broken_router_output_cannot_spawn_unlisted(self):
        write_user_config(use_toml(["copilot"]))
        real = router.route

        def leaky(*a):
            plan, reason = real(*a)
            return {**plan, "agy": plan["copilot"], "codex": plan["copilot"]}, reason
        with mock.patch.object(router, "route", leaky):
            self.stop_with_change()
        self.assertEqual([set(c) for c in self.calls], [{"copilot"}])
        self.calls.clear()
        with mock.patch.object(router, "route", lambda *a: ({"agy": real(*a)[0]["copilot"]}, None)):
            out = self.stop_with_change()
        self.assert_no_spawn_notice(out, NOTICE_UNAVAILABLE)

    def test_author_and_trigger_filters(self):
        before = lambda: len(state.read_jsonl(state.path("runs.jsonl")))  # noqa: E731
        write_user_config(use_toml(["copilot", "agy"]) + '[authors]\nreview = ["claude"]\n')
        p = {"sessionId": "f1", "cwd": self.repo, "prompt": "write code"}
        self.run_hook("copilot", "turn-start", p)                  # author copilot is not in authors.review
        pathlib.Path(self.repo, "c.py").write_text("z = 1\n" * 20)
        n = before()
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.assertEqual(self.run_hook("copilot", "turn-end", {"sessionId": "f1", "cwd": self.repo}), {})
        self.assertEqual((self.calls, before()), ([], n))
        write_user_config(use_toml(["copilot", "agy"]) + '[triggers]\nkinds = ["code"]\n')
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="plan it"))
        pathlib.Path(self.repo, "tasks").mkdir()
        pathlib.Path(self.repo, "tasks", "todo.md").write_text("- step\n" * 20)
        n = before()
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.assertEqual(self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False)), {})
        self.assertEqual((self.calls, before()), ([], n))

    def test_plan_gate_without_list_is_not_reviewed(self):
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            out = self.run_hook("claude", "x", {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode",
                                                "session_id": "sess1", "cwd": self.repo,
                                                "tool_input": {"plan": "1. do a thing\n2. do another\n"}})
        self.assertEqual(self.calls, [])
        self.assertIn("no reviewers configured", str(out))


if __name__ == "__main__":
    unittest.main()
