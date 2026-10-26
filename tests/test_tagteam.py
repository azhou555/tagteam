import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TMP_HOME = tempfile.mkdtemp(prefix="tagteam-test-")
os.environ["TAGTEAM_HOME"] = TMP_HOME
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tagteam import adapters, changes, classify, core, router, runner, state, verdict  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
CFG = state.load_config()


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_repo():
    d = tempfile.mkdtemp()
    sh(d, "git", "init", "-q")
    pathlib.Path(d, "a.py").write_text("x = 1\n")
    sh(d, "git", "add", "-A")
    sh(d, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return os.path.realpath(d)


class Adapters(unittest.TestCase):
    def test_claude_phases(self):
        self.assertEqual(adapters.parse("claude", "x", fx("claude-prompt")).phase, "turn-start")
        ev = adapters.parse("claude", "x", fx("claude-touch-Write"))
        self.assertEqual((ev.phase, [os.path.basename(p) for p in ev.paths]), ("touch", ["hello.txt"]))
        self.assertEqual(adapters.parse("claude", "x", fx("claude-touch-Bash")).paths, [])
        stop = adapters.parse("claude", "x", fx("claude-stop-1"))
        self.assertEqual((stop.phase, stop.stop_hook_active, stop.last_message), ("turn-end", True, "done"))

    def test_claude_adapter_ignores_copilot_running_claude_hooks(self):
        for name in ("copilot_as_claude-prompt", "copilot_as_claude-touch-Edit", "copilot_as_claude-stop"):
            self.assertIsNone(adapters.parse("claude", "x", fx(name)), name)

    def test_codex_payload_routed_to_codex_only(self):
        p = {"hook_event_name": "Stop", "session_id": "s", "turn_id": "t1", "cwd": "/r", "model": "m",
             "transcript_path": "/Users/u/.codex/sessions/x.jsonl", "stop_hook_active": False,
             "last_assistant_message": "ok", "permission_mode": "default"}
        self.assertIsNone(adapters.parse("claude", "x", p))
        ev = adapters.parse("codex", "x", p)
        self.assertEqual((ev.agent, ev.phase, ev.turn_id), ("codex", "turn-end", "t1"))

    def test_copilot_native(self):
        ev = adapters.parse("copilot", "touch", fx("copilot-touch-apply_patch"))
        self.assertEqual([os.path.basename(p) for p in ev.paths], ["hello.txt"])
        self.assertTrue(adapters.parse("copilot", "turn-end", fx("copilot-stop-1")).stop_hook_active)

    def test_agy(self):
        ev = adapters.parse("agy", "turn-start", fx("agy-prompt"))
        self.assertEqual(ev.phase, "turn-start")
        self.assertTrue(ev.prompt.startswith("Create a file"))
        self.assertTrue(ev.cwd.endswith("ws-agy"))                       # workspace, not .agents dir
        self.assertEqual(adapters.parse("agy", "turn-start", fx("agy-prompt-1")).phase, "ignore")
        self.assertEqual([os.path.basename(p) for p in adapters.parse("agy", "touch", fx("agy-touch-write_file")).paths],
                         ["hello.txt"])
        self.assertFalse(adapters.parse("agy", "turn-end", fx("agy-stop")).stop_hook_active)
        self.assertTrue(adapters.parse("agy", "turn-end", fx("agy-stop-1")).stop_hook_active)

    def test_encode(self):
        block = {"action": "block", "message": "m"}
        self.assertEqual(adapters.encode("claude", "turn-end", None), "{}")
        self.assertEqual(json.loads(adapters.encode("copilot", "turn-end", block)), {"decision": "block", "reason": "m"})
        self.assertEqual(json.loads(adapters.encode("agy", "turn-end", block))["decision"], "continue")
        gate = json.loads(adapters.encode("claude", "plan-gate", block))
        self.assertEqual(gate["hookSpecificOutput"]["permissionDecision"], "deny")


class Changes(unittest.TestCase):
    def test_snapshot_diff_catches_shell_writes_and_leaves_index_alone(self):
        repo = make_repo()
        base = changes.snapshot(repo)
        pathlib.Path(repo, "new.py").write_text("print(1)\nprint(2)\n")       # untracked, as a shell write would
        pathlib.Path(repo, "a.py").write_text("x = 2\n")
        pathlib.Path(repo, "pkg.lock").write_text("ignored\n")
        head = changes.snapshot(repo)
        got = {a["path"]: (a["status"], a["lines_changed"]) for a in changes.diff_trees(repo, base, head, ["*.lock"])}
        self.assertEqual(got, {"new.py": ("added", 2), "a.py": ("modified", 2)})
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(staged.stdout, "")

    def test_delete(self):
        repo = make_repo()
        base = changes.snapshot(repo)
        os.remove(os.path.join(repo, "a.py"))
        arts = changes.diff_trees(repo, base, changes.snapshot(repo), [])
        self.assertEqual([(a["path"], a["status"]) for a in arts], [("a.py", "deleted")])


class Classify(unittest.TestCase):
    def test_kinds(self):
        k = lambda p: classify.kind(p, CFG)  # noqa: E731
        self.assertEqual([k("tasks/todo.md"), k("docs/arch.md"), k("DESIGN.md"), k("src/app.py"),
                          k("/Users/u/.claude/plans/foo.md")], ["plan", "design", "design", "code", "plan"])
        self.assertEqual(classify.annotate([{"path": "db/migrations/1.sql"}], CFG)[0]["risk"], "high")


def art(path, lines=10, risk="normal", kind="code"):
    return {"path": path, "lines_changed": lines, "risk": risk, "kind": kind, "diff": "", "status": "modified"}


FULL = {"claude": 0.9, "codex": 0.9, "copilot": 0.9, "agy": 0.9}


class Router(unittest.TestCase):
    def test_reviewer_tier_takes_all_normal_work(self):
        for author in ("claude", "codex"):
            for groups in ({"code": [art("a.py")]}, {"code": [art("a.py", 500)]},
                           {"plan": [art("p.md", kind="plan")]}, {"design": [art("d.md", kind="design")]}):
                plan = router.route(author, groups, CFG, FULL, {})
                self.assertTrue(set(plan) <= {"copilot", "agy"}, (author, groups, plan))

    def test_high_risk_adds_primary_with_headroom_never_author(self):
        plan = router.route("claude", {"code": [art("auth.py", risk="high")]}, CFG, FULL, {})
        self.assertEqual(set(plan), {"copilot", "agy", "codex"})
        low = {**FULL, "codex": 0.3}                                     # below 50% reserve floor
        self.assertEqual(set(router.route("claude", {"code": [art("auth.py", risk="high")]}, CFG, low, {})),
                         {"copilot", "agy"})

    def test_primary_with_unknown_quota_is_never_used(self):
        h = {**FULL, "claude": None}                                     # e.g. stale status line cache
        self.assertEqual(set(router.route("codex", {"code": [art("auth.py", risk="high")]}, CFG, h, {})),
                         {"copilot", "agy"})
        h2 = {**FULL, "agy": None}                                       # unknown reviewer-tier is still usable
        self.assertIn("agy", router.route("claude", {"plan": [art("p.md", kind="plan")]}, CFG, h2, {}))

    def test_reviewer_author_gets_other_reviewer_first(self):
        plan = router.route("copilot", {"code": [art("a.py")]}, CFG, FULL, {})
        self.assertEqual(list(plan), ["agy"])
        plan = router.route("copilot", {"plan": [art("p.md", kind="plan")]}, CFG, FULL, {})
        self.assertEqual(set(plan), {"agy", "claude"})                   # tie on headroom -> name order

    def test_fallback_to_primary_when_reviewers_exhausted(self):
        h = {**FULL, "copilot": 0.0, "agy": 0.02}
        self.assertEqual(list(router.route("claude", {"code": [art("a.py")]}, CFG, h, {})), ["codex"])
        cfg = copy.deepcopy(CFG)
        cfg["routing"]["primary_use"] = "never"
        self.assertEqual(router.route("claude", {"code": [art("a.py")]}, cfg, h, {}), {})

    def test_load_spreads_and_shifts_with_headroom(self):
        load = {}
        for _ in range(300):
            for r in router.route("claude", {"code": [art("a.py")]}, CFG, FULL, load):
                load[r] = load.get(r, 0) + 1
        self.assertEqual(set(load), {"copilot", "agy"})
        self.assertLess(abs(load["copilot"] / load["agy"] - 0.9 / 0.8), 0.05)  # proportional to weight
        load2 = {}
        for _ in range(300):
            for r in router.route("claude", {"code": [art("a.py")]}, CFG, {**FULL, "copilot": 0.2}, load2):
                load2[r] = load2.get(r, 0) + 1
        self.assertGreater(load2["agy"], 3 * load2["copilot"])

    def test_distinct_lenses(self):
        plan = router.route("claude", {"design": [art("d.md", kind="design")]}, CFG, FULL, {})
        self.assertEqual(sorted(t["lens"] for ts in plan.values() for t in ts), ["correctness", "design"])


def finding(sev="major", conf=0.9, claim="off by one in loop", line=10, file="a.py"):
    return {"severity": sev, "file": file, "line": line, "claim": claim, "evidence": "e",
            "suggested_fix": "f", "confidence": conf}


class Verdict(unittest.TestCase):
    def test_dedupe_and_threshold(self):
        res = {"copilot": {"findings": [finding(), finding("nit", claim="rename var", line=40)]},
               "agy": {"findings": [finding("blocker", claim="Off by one in the loop", line=11)]}}
        action, blocking, advisory = verdict.decide(res, CFG, 1)
        self.assertEqual(action, "block")
        self.assertEqual(len(blocking), 1)
        self.assertEqual((blocking[0]["severity"], blocking[0]["reviewers"]), ("blocker", ["agy", "copilot"]))
        self.assertEqual(len(advisory), 1)

    def test_low_confidence_and_round_cap_allow(self):
        self.assertEqual(verdict.decide({"agy": {"findings": [finding(conf=0.3)]}}, CFG, 1)[0], "allow")
        self.assertEqual(verdict.decide({"agy": {"findings": [finding()]}}, CFG, 2)[0], "block")
        self.assertEqual(verdict.decide({"agy": {"findings": [finding()]}}, CFG, 3)[0], "allow")

    def test_failed_reviewer_does_not_block(self):
        self.assertEqual(verdict.decide({"agy": None}, CFG, 1)[0], "allow")


class Runner(unittest.TestCase):
    def test_parse_formats(self):
        payload = {"verdict": "changes_requested", "summary": "s", "findings": [finding()]}
        claude = json.dumps({"result": json.dumps(payload), "structured_output": payload})
        agy = json.dumps({"status": "SUCCESS", "response": json.dumps(payload), "structured_output": payload})
        copilot = "Here you go:\n```json\n" + json.dumps(payload) + "\n```"
        for agent, out in (("claude", claude), ("agy", agy), ("copilot", copilot)):
            self.assertEqual(runner.parse_output(agent, out, None)["findings"][0]["claim"], "off by one in loop", agent)
        self.assertIsNone(runner.parse_output("copilot", "no json here", None))

    def test_normalize_repairs_bad_fields(self):
        r = runner.normalize({"findings": [{"claim": "x", "severity": "huge", "confidence": "?"}]})
        self.assertEqual((r["verdict"], r["findings"][0]["severity"], r["findings"][0]["confidence"]),
                         ("changes_requested", "minor", 0.5))

    def test_prompt_contains_diff_and_round2_context(self):
        p = runner.build_prompt("agy", "claude", "/r", "do X", [{"kind": "code", "lens": "correctness",
                                "artifacts": [art("a.py") | {"diff": "+bug"}]}], 1000,
                                prior=[finding()], author_reply="I fixed it")
        for s in ("do X", "+bug", "Round 2", "I fixed it", "READ-ONLY", "agy", "claude"):
            self.assertIn(s, p)


class EndToEnd(unittest.TestCase):
    """Real hook flow through cli.hook with the reviewer CLIs stubbed."""

    def run_hook(self, agent, phase, payload):
        from tagteam import cli
        with mock.patch("sys.stdin", new=__import__("io").StringIO(json.dumps(payload))), \
                mock.patch("builtins.print") as pr:
            cli.hook(agent, phase)
        return json.loads(pr.call_args[0][0])

    def setUp(self):
        self.repo = make_repo()
        self.calls = []
        self.p = mock.patch.object(core, "headroom_map", return_value=FULL)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def fake_run_all(self, results):
        def run_all(jobs, cfg, cwd, run_dir, deadline):
            self.calls.append(jobs)
            return {r: ({"reviewer": r, "ok": True, "duration": 1}, results.get(r, {"verdict": "approve", "summary": "",
                                                                                  "findings": []})) for r in jobs}
        return run_all

    def claude(self, event, **kw):
        return {"hook_event_name": event, "session_id": "sess1", "cwd": self.repo, "prompt_id": "p",
                "transcript_path": "/Users/u/.claude/projects/x.jsonl", **kw}

    def test_block_then_fix_then_allow(self):
        self.assertEqual(self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature")), {})
        pathlib.Path(self.repo, "feature.py").write_text("def f(x):\n    return x[0]\n" * 4)
        bad = {"verdict": "changes_requested", "summary": "", "findings": [finding(file="feature.py", line=2)]}
        with mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": bad})):
            out = self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False, last_assistant_message="done"))
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("[tagteam]"))
        self.assertEqual(set(self.calls[0]), {"copilot"} if len(self.calls[0]) == 1 else set(self.calls[0]))
        self.assertTrue(set(self.calls[0]) <= {"copilot", "agy"})
        # author fixes; round 2 sees only the delta and the prior finding
        pathlib.Path(self.repo, "feature.py").write_text("def f(x):\n    return x[0] if x else None\n" * 4)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            out = self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=True, last_assistant_message="fixed"))
        self.assertEqual(out, {})
        prompt = next(iter(self.calls[1].values()))
        self.assertIn("Round 2", prompt)
        self.assertIn("fixed", prompt)
        self.assertIn("x[0] if x else None", prompt)

    def test_one_line_fix_is_still_rechecked_in_round_2(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        pathlib.Path(self.repo, "g.py").write_text("a = 1\n" * 10)
        bad = {"verdict": "changes_requested", "summary": "", "findings": [finding(file="g.py", line=1)]}
        with mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": bad, "agy": bad})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        pathlib.Path(self.repo, "g.py").write_text("a = 2\n" + "a = 1\n" * 9)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=True))
        self.assertEqual(len(self.calls), 2)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):   # nothing new -> no third review
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=True))
        self.assertEqual(len(self.calls), 2)

    def test_no_changes_no_review_and_depth_guard(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="question only"))
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.assertEqual(self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False)), {})
        self.assertEqual(self.calls, [])
        with mock.patch.dict(os.environ, {"TAGTEAM_DEPTH": "1"}):
            pathlib.Path(self.repo, "big.py").write_text("y = 1\n" * 50)
            with mock.patch.object(runner, "run_all", self.fake_run_all({})):
                self.assertEqual(self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False)), {})
        self.assertEqual(self.calls, [])

    def test_copilot_reinjected_block_message_keeps_baseline(self):
        p = {"sessionId": "c1", "cwd": self.repo, "prompt": "write code"}
        self.run_hook("copilot", "turn-start", p)
        pathlib.Path(self.repo, "c.py").write_text("z = 1\n" * 20)
        bad = {"verdict": "changes_requested", "summary": "", "findings": [finding(file="c.py")]}
        with mock.patch.object(runner, "run_all", self.fake_run_all({"agy": bad})):
            out = self.run_hook("copilot", "turn-end", {"sessionId": "c1", "cwd": self.repo})
        self.assertEqual(out["decision"], "block")
        self.run_hook("copilot", "turn-start", {**p, "prompt": out["reason"]})   # Copilot re-submits it
        s = state.read_json(state.path("sessions", "copilot-c1.json"))
        self.assertEqual(s["round"], 1)

    def test_reviewer_crash_fails_open(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="x"))
        pathlib.Path(self.repo, "f.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False)), {})


if __name__ == "__main__":
    unittest.main()
