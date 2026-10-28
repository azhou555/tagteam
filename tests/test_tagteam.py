import contextlib
import copy
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from unittest import mock

from tests.helpers import CFG, FULL, ROOT, HookRig, art, finding, fx, make_repo, sh, use_toml, write_user_config  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import adapters, changes, classify, core, harnesses, install, live, router, runner, state, verdict  # noqa: E402
from tagteam.harnesses import agy as agy_h, base as harness_base, claude as claude_h, codex as codex_h, copilot as copilot_h  # noqa: E402


class Adapters(unittest.TestCase):
    def test_claude_phases(self):
        self.assertEqual(adapters.parse("claude", "x", fx("claude-prompt")).phase, "turn-start")
        ev = adapters.parse("claude", "x", fx("claude-touch-Write"))
        self.assertEqual((ev.phase, [os.path.basename(p) for p in ev.paths]), ("touch", ["hello.txt"]))
        self.assertEqual(adapters.parse("claude", "x", fx("claude-touch-Bash")).paths, [])
        stop = adapters.parse("claude", "x", fx("claude-stop-1"))
        self.assertEqual((stop.phase, stop.last_message), ("turn-end", "done"))

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
        self.assertEqual(adapters.parse("copilot", "turn-end", fx("copilot-stop-1")).phase, "turn-end")

    def test_agy(self):
        ev = adapters.parse("agy", "turn-start", fx("agy-prompt"))
        self.assertEqual(ev.phase, "turn-start")
        self.assertTrue(ev.prompt.startswith("Create a file"))
        self.assertTrue(ev.cwd.endswith("ws-agy"))                       # workspace, not .agents dir
        self.assertEqual(adapters.parse("agy", "turn-start", fx("agy-prompt-1")).phase, "ignore")
        self.assertEqual([os.path.basename(p) for p in adapters.parse("agy", "touch", fx("agy-touch-write_file")).paths],
                         ["hello.txt"])
        self.assertEqual(adapters.parse("agy", "turn-end", fx("agy-stop-1")).phase, "turn-end")

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

    def test_snapshot_tolerates_crlf_under_safecrlf(self):
        repo = make_repo()
        sh(repo, "git", "config", "core.autocrlf", "input")
        sh(repo, "git", "config", "core.safecrlf", "true")
        base = changes.snapshot(repo)
        pathlib.Path(repo, "win.h").write_bytes(b"int x;\r\nint y;\r\n")
        arts = changes.diff_trees(repo, base, changes.snapshot(repo), [])
        self.assertEqual([(a["path"], a["status"]) for a in arts], [("win.h", "added")])

    def test_snapshot_skips_untracked_nested_repo_but_keeps_tracked_submodule(self):
        repo = make_repo()
        sub = make_repo()
        sh(repo, "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", sub, "vendored")
        sh(repo, "git", "commit", "-qm", "submodule")
        base = changes.snapshot(repo)
        sh(sub, "git", "commit", "-q", "--allow-empty", "-m", "bump")
        sh(os.path.join(repo, "vendored"), "git", "pull", "-q", "origin", "HEAD")
        os.rename(make_repo(), os.path.join(repo, "worktree"))
        sh(repo, "git", "init", "-q", os.path.join(repo, "deep", "unborn"))
        pathlib.Path(repo, "new.py").write_text("x\n")
        arts = changes.diff_trees(repo, base, changes.snapshot(repo), [])
        self.assertEqual(sorted((a["path"], a["status"]) for a in arts),
                         [("new.py", "added"), ("vendored", "modified")])


    def test_snapshot_sees_same_size_rewrite_in_the_index_second(self):
        """Racy git: a same-size rewrite in the second the index was written looks clean to a stat check,
        unless the temp index keeps the real index's mtime so git knows to re-hash the file."""
        repo = make_repo()
        sh(repo, "git", "config", "core.trustctime", "false")        # utime and the rewrite both move ctime
        sh(repo, "git", "config", "core.checkStat", "minimal")
        f, index, second = os.path.join(repo, "a.py"), os.path.join(repo, ".git", "index"), int(time.time()) - 100
        os.utime(f, (second, second))
        sh(repo, "git", "update-index", "--refresh")                  # the index now records mtime `second`
        os.utime(index, (second, second))
        with open(f, "r+") as fh:
            fh.write("y = 2\n")                                       # same size as "x = 1\n"
        os.utime(f, (second, second))
        tree = changes.snapshot(repo)
        want = subprocess.run(["git", "hash-object", "a.py"], cwd=repo, capture_output=True, text=True).stdout.strip()
        got = subprocess.run(["git", "ls-tree", tree, "a.py"], cwd=repo, capture_output=True, text=True).stdout.split()
        self.assertEqual(got[2], want)

class Classify(unittest.TestCase):
    def test_kinds(self):
        k = lambda p: classify.kind(p, CFG)  # noqa: E731
        self.assertEqual([k("tasks/todo.md"), k("docs/arch.md"), k("DESIGN.md"), k("src/app.py"),
                          k("/Users/u/.claude/plans/foo.md")], ["plan", "design", "design", "code", "plan"])
        self.assertEqual(classify.annotate([{"path": "db/migrations/1.sql"}], CFG)[0]["risk"], "high")


QCFG = copy.deepcopy(CFG)
QCFG["routing"]["mode"] = "quota"
QCFG["reviewers"]["use"] = ["claude", "codex", "copilot", "agy"]


def route(*a):
    return router.route(*a)[0]


class Router(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(harnesses, "detected", return_value=["claude", "codex", "copilot", "agy"])
        p.start()
        self.addCleanup(p.stop)

    def test_reviewer_tier_takes_all_normal_work(self):
        for author in ("claude", "codex"):
            for groups in ({"code": [art("a.py")]}, {"code": [art("a.py", 500)]},
                           {"plan": [art("p.md", kind="plan")]}, {"design": [art("d.md", kind="design")]}):
                plan = route(author, groups, QCFG, FULL, {})
                self.assertTrue(set(plan) <= {"copilot", "agy"}, (author, groups, plan))

    def test_high_risk_adds_primary_with_headroom_never_author(self):
        cfg = copy.deepcopy(QCFG)
        cfg["review"]["per_change"]["high_risk"] = 3                    # default is 2; 3 reaches a primary
        plan = route("claude", {"code": [art("auth.py", risk="high")]}, cfg, FULL, {})
        self.assertEqual(set(plan), {"copilot", "agy", "codex"})
        low = {**FULL, "codex": 0.3}                                     # below 50% reserve floor
        self.assertEqual(set(route("claude", {"code": [art("auth.py", risk="high")]}, cfg, low, {})),
                         {"copilot", "agy"})

    def test_unlisted_agent_is_never_picked(self):
        cfg = copy.deepcopy(QCFG)
        cfg["reviewers"]["use"] = ["agy", "claude", "codex"]
        plan = route("claude", {"design": [art("d.md", kind="design")]}, cfg, FULL, {})
        self.assertNotIn("copilot", plan)
        self.assertTrue(plan)

    def test_primary_with_unknown_quota_is_never_used(self):
        h = {**FULL, "claude": None}                                     # e.g. stale status line cache
        self.assertEqual(set(route("codex", {"code": [art("auth.py", risk="high")]}, QCFG, h, {})),
                         {"copilot", "agy"})
        h2 = {**FULL, "agy": None}                                       # unknown reviewer-tier is still usable
        self.assertIn("agy", route("claude", {"plan": [art("p.md", kind="plan")]}, QCFG, h2, {}))

    def test_reviewer_author_gets_other_reviewer_first(self):
        plan = route("copilot", {"code": [art("a.py")]}, QCFG, FULL, {})
        self.assertEqual(list(plan), ["agy"])
        plan = route("copilot", {"plan": [art("p.md", kind="plan")]}, QCFG, FULL, {})
        self.assertEqual(set(plan), {"agy", "claude"})                   # tie on headroom -> name order

    def test_fallback_to_primary_when_reviewers_exhausted(self):
        h = {**FULL, "copilot": 0.0, "agy": 0.02}
        self.assertEqual(list(route("claude", {"code": [art("a.py")]}, QCFG, h, {})), ["codex"])
        cfg = copy.deepcopy(QCFG)
        cfg["routing"]["quota"]["primary_use"] = "never"
        self.assertEqual(route("claude", {"code": [art("a.py")]}, cfg, h, {}), {})

    def test_load_spreads_and_shifts_with_headroom(self):
        load = {}
        for _ in range(300):
            for r in route("claude", {"code": [art("a.py")]}, QCFG, FULL, load):
                load[r] = load.get(r, 0) + 1
        self.assertEqual(set(load), {"copilot", "agy"})
        self.assertLess(abs(load["copilot"] / load["agy"] - 0.9 / 0.8), 0.05)  # proportional to weight
        load2 = {}
        for _ in range(300):
            for r in route("claude", {"code": [art("a.py")]}, QCFG, {**FULL, "copilot": 0.2}, load2):
                load2[r] = load2.get(r, 0) + 1
        self.assertGreater(load2["agy"], 3 * load2["copilot"])

    def test_distinct_lenses(self):
        plan = route("claude", {"design": [art("d.md", kind="design")]}, QCFG, FULL, {})
        self.assertEqual(sorted(t["lens"] for ts in plan.values() for t in ts), ["correctness", "design"])


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
            self.assertEqual(harnesses.get(agent).reviewer.parse(out, None)["findings"][0]["claim"], "off by one in loop", agent)
        self.assertIsNone(harnesses.get("copilot").reviewer.parse("no json here", None))

    def test_normalize_repairs_bad_fields(self):
        r = harness_base.normalize({"findings": [{"claim": "x", "severity": "huge", "confidence": "?"}]})
        self.assertEqual((r["verdict"], r["findings"][0]["severity"], r["findings"][0]["confidence"]),
                         ("changes_requested", "minor", 0.5))

    def test_prompt_contains_diff_and_round2_context(self):
        p = runner.build_prompt("agy", "claude", "/r", "do X", [{"kind": "code", "lens": "correctness",
                                "artifacts": [art("a.py") | {"diff": "+bug"}]}], 1000,
                                prior=[finding()], author_reply="I fixed it")
        for s in ("do X", "+bug", "Round 2", "I fixed it", "READ-ONLY", "agy", "claude"):
            self.assertIn(s, p)


class EndToEnd(HookRig, unittest.TestCase):
    """Real hook flow through cli.hook with the reviewer CLIs stubbed."""

    def setUp(self):
        self.rig(use_toml(["copilot", "agy"]))

    def tearDown(self):
        self.unrig()

    def review_racing_a_new_turn(self, payload):
        """Run `payload` while the user starts a new turn mid-review; return (output, session before, after)."""
        bad = {"verdict": "changes_requested", "summary": "", "findings": [finding(file="feature.py", line=2)]}
        blocking = self.fake_run_all({"copilot": bad, "agy": bad})
        sf = core._session_file(adapters.parse("claude", "turn-start", self.claude("UserPromptSubmit", prompt="x")))
        new_turn = {}

        def run_all(*a, **kw):
            self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="next request"))
            new_turn.update(state.read_json(sf))
            return blocking(*a, **kw)
        with mock.patch.object(runner, "run_all", run_all):
            out = self.run_hook("claude", "x", payload)
        return out, new_turn, state.read_json(sf)

    def test_turn_started_mid_review_keeps_its_state_and_inherits_the_open_issues(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        pathlib.Path(self.repo, "feature.py").write_text("def f(x):\n    return x[0]\n" * 4)
        out, new_turn, after = self.review_racing_a_new_turn(self.claude("Stop", last_assistant_message="done"))
        self.assertEqual(out["decision"], "block")                       # the verdict still reaches the author
        rest = lambda d: {k: v for k, v in d.items() if k not in ("prior", "carried")}
        self.assertEqual(rest(after), rest(new_turn))
        self.assertEqual((after["prompt"], after["round"]), ("next request", 0))
        self.assertEqual(([(f["file"], f["claim"]) for f in after["prior"]], after["carried"]),
                         ([("feature.py", "off by one in loop")], True))
        logged = [r for r in state.read_jsonl(state.path("tagteam.log"))
                  if r["msg"] == "new turn started during review; session state not updated"]
        self.assertEqual(len(logged), 1)

    def test_plan_gate_leaves_a_turn_started_mid_review_alone(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="plan it"))
        plan = self.claude("PreToolUse", tool_name="ExitPlanMode", tool_input={"plan": "1. a\n2. b\n"})
        out, new_turn, after = self.review_racing_a_new_turn(plan)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(after, new_turn)
        self.assertEqual((after["plan_rounds"], after["plan_prior"]), (0, []))

    def test_repo_reached_through_a_symlink_is_not_outside(self):
        link = os.path.join(tempfile.mkdtemp(), "link")
        os.symlink(self.repo, link)
        path = os.path.join(link, "feature.py")
        self.assertTrue(changes.in_repo(path, self.repo))
        self.assertFalse(changes.in_repo(self.repo + "-other/feature.py", self.repo))
        self.run_hook("claude", "x", {**self.claude("UserPromptSubmit", prompt="add feature"), "cwd": link})
        pathlib.Path(path).write_text("a = 1\n" * 20)
        self.run_hook("claude", "x", {**self.claude("PostToolUse", tool_name="Write", tool_input={"file_path": path}),
                                      "cwd": link})
        sf = core._session_file(adapters.parse("claude", "turn-start", self.claude("UserPromptSubmit", prompt="x")))
        self.assertEqual(state.read_json(sf)["outside_touched"], [])
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", {**self.claude("Stop"), "cwd": link})
        prompt = next(iter(self.calls[0].values()))
        self.assertIn("### feature.py (added", prompt)
        self.assertNotIn("full content of", prompt)                     # not also sent as an outside-repo file

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
        self.assertTrue(out["systemMessage"].startswith("[tagteam] ✓ Issues resolved: all 1 fixed and re-checked"), out)
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
        self.assertEqual(live.active(), [])                    # crash still clears the running record

    def test_review_is_live_while_running_then_recorded(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="x"))
        pathlib.Path(self.repo, "f.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            out = self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        [running] = self.seen_active[0]
        self.assertEqual((running["author"], running["session_id"], running["round"]), ("claude", "sess1", 1))
        self.assertEqual(live.active(), [])
        last = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((last["author"], last["session_id"], last["action"]), ("claude", "sess1", "allow"))
        self.assertIn("reviewed 20 changed lines", out["systemMessage"])
        self.assertTrue(live.segment("claude", "sess1").startswith("tagteam ✓ Review passed: no issues found · just now · "))

    def test_git_activity_reaches_prompt_but_never_triggers_review(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="commit it"))
        sh(self.repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "c2")
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.assertEqual(self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False)), {})
        self.assertEqual(self.calls, [])                       # ref move alone: no review
        pathlib.Path(self.repo, "f.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        prompt = next(iter(self.calls[0].values()))
        self.assertIn("## Git activity this turn", prompt)
        self.assertIn("1 new commit(s)", prompt)
        self.assertIn("c2", prompt)

    def test_copilot_allow_stays_silent(self):
        p = {"sessionId": "c2", "cwd": self.repo, "prompt": "write code"}
        self.run_hook("copilot", "turn-start", p)
        pathlib.Path(self.repo, "c.py").write_text("z = 1\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            self.assertEqual(self.run_hook("copilot", "turn-end", {"sessionId": "c2", "cwd": self.repo}), {})


class GitActivity(unittest.TestCase):
    def commit(self, repo, msg):
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", msg)

    def test_commit_rewrite_push_delete(self):
        repo = make_repo()
        remote = tempfile.mkdtemp()
        sh(remote, "git", "init", "-q", "--bare")
        sh(repo, "git", "remote", "add", "origin", remote)
        sh(repo, "git", "branch", "doomed")
        sh(repo, "git", "push", "-q", "origin", "HEAD")
        old = changes.refs(repo)
        self.commit(repo, "second")
        sh(repo, "git", "push", "-q", "origin", "HEAD")
        self.commit(repo, "third")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--amend", "--allow-empty",
           "-m", "third amended")
        sh(repo, "git", "branch", "-q", "-D", "doomed")
        out = "\n".join(changes.ref_activity(repo, old, changes.refs(repo)))
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True,
                                text=True).stdout.strip()
        self.assertIn(f"refs/heads/{branch}: ", out)
        self.assertIn("2 new commit(s)", out)                   # second + third amended, relative to old
        self.assertIn("third amended", out)
        self.assertIn(f"refs/remotes/origin/{branch}: ", out)   # the push
        self.assertIn("deleted refs/heads/doomed", out)
        mid = changes.refs(repo)
        sh(repo, "git", "reset", "-q", "--hard", "HEAD~1")
        self.commit(repo, "replacement")
        self.assertIn("REWRITTEN", "\n".join(changes.ref_activity(repo, mid, changes.refs(repo))))


class Live(unittest.TestCase):
    def setUp(self):
        for p in [state.path("runs.jsonl"), state.path("status.json"), *live._active_dir().glob("*")]:
            p.unlink(missing_ok=True)

    def rec(self, run_id="r1", **kw):
        now = time.time()
        return {"run_id": run_id, "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
                "max_rounds": 2, "reviewers": ["copilot", "agy"], "started": now, "expires": now + 300, **kw}

    def done_run(self, **kw):
        return {"ts": time.time(), "run_id": "x", "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
                "max_rounds": 2, "reviewers": ["copilot"], "action": "allow", "blocking": 0, "advisory": 0, "failed": [],
                "duration": 3.0, **kw}

    def test_tracking_lifecycle_and_progress(self):
        with live.tracking(self.rec()) as update:
            update("copilot", {"ok": True, "duration": 11})
            [r] = live.active()
            self.assertEqual(r["reviewers"]["copilot"], {"state": "done", "secs": 11})
            seg = live.segment("claude", "s")
            self.assertIn("tagteam ⟳ Reviewing changes, round 1/2", seg)
            self.assertIn("copilot ✓ 11s", seg)
            self.assertIn("agy …", seg)
            self.assertEqual(live.segment("claude", "other").count("+1 running elsewhere"), 1)
        self.assertEqual(live.active(), [])
        with self.assertRaises(RuntimeError), live.tracking(self.rec("r2")):
            raise RuntimeError("boom")
        self.assertEqual(list(live._active_dir().glob("*")), [])

    def test_killed_owner_and_expired_record_are_stale(self):
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); from tagteam import live; "
                "cm = live.tracking(eval(sys.argv[2])); cm.__enter__(); os.kill(os.getpid(), signal.SIGKILL)")
        subprocess.run([sys.executable, "-c", code, str(ROOT), repr(self.rec("dead"))])
        self.assertTrue((live._active_dir() / "dead.json").exists())     # SIGKILL skipped cleanup
        self.assertEqual(live.active(), [])
        self.assertFalse((live._active_dir() / "dead.json").exists())
        with live.tracking(self.rec("old", expires=time.time() - 1)) as update:
            self.assertEqual(live.active(), [])                          # backstop even while lock is held
            update("copilot", {"ok": True, "duration": 1})               # the owner rewrites its dropped record
            self.assertEqual(live.active(), [])
        self.assertEqual([r["run_id"] for r in state.read_jsonl(state.path("runs.jsonl"))], ["dead"])   # expiry alone records nothing

    def test_killed_owner_is_recorded_once_by_concurrent_readers(self):
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); from tagteam import live; "
                "cm = live.tracking(eval(sys.argv[2])); cm.__enter__(); os.kill(os.getpid(), signal.SIGKILL)")
        subprocess.run([sys.executable, "-c", code, str(ROOT), repr(self.rec("dead", prior=2))])
        read, raced = state.read_json, []

        def second_reader_interleaves(p, default=None):      # both readers see the record before either claims it
            rec = read(p, default)
            if p.parent == live._active_dir() and not raced:
                raced.append(True)
                self.assertEqual(live.active(), [])
            return rec
        with mock.patch.object(state, "read_json", second_reader_interleaves):
            self.assertEqual(live.active(), [])
        self.assertEqual(raced, [True])
        [run] = state.read_jsonl(state.path("runs.jsonl"))
        self.assertEqual((run["run_id"], run["reason"], run["reviewers"], run["prior"]), ("dead", "interrupted", ["copilot", "agy"], 2))
        self.assertEqual(list(live._active_dir().glob("*")), [])
        self.assertTrue(live.segment("claude", "s").startswith(
            "tagteam ⚠ Review interrupted: stopped before reviewers finished · 2 earlier issues not re-checked · "))
        self.assertIn("⚠ Review interrupted", live.render_watch())
        self.assertIn(f"Review interrupted: stopped before reviewers finished · 2 earlier issues not re-checked · blocking "
                      f"| color={live.KINDS['interrupted']['hex']}", live.render_swiftbar())

    def test_killed_review_lands_before_a_later_run(self):
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); from tagteam import live; "
                "cm = live.tracking(eval(sys.argv[2])); cm.__enter__(); os.kill(os.getpid(), signal.SIGKILL)")
        subprocess.run([sys.executable, "-c", code, str(ROOT), repr(self.rec("dead"))])
        live.record_run(self.done_run())
        self.assertEqual([r["run_id"] for r in state.read_jsonl(state.path("runs.jsonl"))], ["dead", "x"])
        self.assertTrue(live.segment("claude", "s").startswith("tagteam ✓ Review passed: no issues found · "))

    def test_concurrent_finishes_are_all_counted(self):
        code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from tagteam import live; "
                "live.record_run(dict(ts=time.time(), run_id=sys.argv[2], author='codex', session_id=sys.argv[2], "
                "tag='turn', round=1, reviewers=['agy'], action='allow', blocking=0, advisory=0, failed=[], "
                "duration=1.0))")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), f"p{i}"]) for i in range(12)]
        for pr in procs:
            pr.wait()
        self.assertEqual(len(state.read_jsonl(state.path("runs.jsonl"))), 12)
        s = state.read_json(state.path("status.json"))
        self.assertEqual((s["today"], len(s["sessions"])), (12, 12))

    def test_idle_segments(self):
        self.assertEqual(live.segment("claude", "s"), "tagteam no review yet this session · 0 reviews today")
        live.record_run(self.done_run(advisory=2))
        self.assertEqual(live.segment("claude", "s"),
                         "tagteam ✓ Review passed: no blocking issues, 2 minor notes · just now · 1 review today")
        live.record_run(self.done_run(action="block", blocking=2))
        live.note_error()
        self.assertEqual(live.segment("claude", "s"), "tagteam ✗ Changes requested: 2 issues to fix (round 1/2) · "
                                                      "just now · 2 reviews today · ⚠ 1 hook error (24h)")
        live.record_run(self.done_run(failed=["copilot"]))
        self.assertIn("⚠ Review failed: every reviewer errored or timed out", live.segment("claude", "s"))
        live.record_run(self.done_run(reviewers=[]))
        self.assertIn("⚠ Not reviewed: no reviewer had quota available", live.segment("claude", "s"))
        self.assertEqual(live.segment("claude", "never-reviewed"),
                         "tagteam no review yet this session · 3 reviews today · ⚠ 1 hook error (24h)")

    def test_colored_segment_uses_sage_brand_and_pastel_text(self):
        live.record_run(self.done_run(action="block", blocking=1))
        seg = live.segment("claude", "s", color=True)
        self.assertTrue(seg.startswith("\x1b[38;5;108mtagteam\x1b[0m "), seg)
        self.assertIn("\x1b[38;5;217m✗ Changes requested: 1 issue to fix (round 1/2)\x1b[0m", seg)
        self.assertNotIn("48;5", seg)                                        # no background blocks
        with live.tracking(self.rec()):
            self.assertIn("\x1b[38;5;223m⟳ Reviewing changes, round 1/2\x1b[0m", live.segment("claude", "s", color=True))

    def test_swiftbar_states(self):
        self.assertTrue(live.render_swiftbar().startswith(
            ':person.2.fill: :checkmark.seal: 0 | tooltip="tagteam: no reviews yet" sfcolor1=' + live.BRAND["hex"]))
        live.record_run(self.done_run(action="block", blocking=1))
        out = live.render_swiftbar()
        rose = live.KINDS["blocked"]["hex"]
        self.assertTrue(out.startswith(":person.2.fill: :xmark.octagon.fill: 1 | "), out)
        self.assertIn(f"sfcolor2={rose}", out.splitlines()[0])
        self.assertIn(f"Changes requested: 1 issue to fix (round 1/2) · blocking | color={rose} bash=/usr/bin/open", out)
        live.note_error()
        head = live.render_swiftbar().splitlines()[0]
        self.assertIn(":exclamationmark.triangle.fill:", head)
        self.assertIn(f"sfcolor3={rose}", head)
        with live.tracking(self.rec()):
            head = live.render_swiftbar().splitlines()[0]
        self.assertTrue(head.startswith(":person.2.fill: :arrow.triangle.2.circlepath: 0s"), head)
        self.assertIn(f"sfcolor2={live.KINDS['running']['hex']}", head)

    def test_yesterday_does_not_count_today(self):
        live.record_run(self.done_run(ts=time.time() - 2 * 86400), now=time.time() - 2 * 86400)
        self.assertIn("0 reviews today", live.segment("claude", "s"))
        live.record_run(self.done_run(ts=time.time() - 86400))      # started yesterday, finished now
        self.assertIn("1 review today", live.segment("claude", "s"))

    def test_backfill_of_older_days_reads_as_zero_today(self):
        with mock.patch.object(state, "STATE", pathlib.Path(tempfile.mkdtemp())):
            for name in ("20200101T120000-aaaa", "20200102T120000-bbbb"):
                state.write_json(state.path("runs", name, "request.json"),
                                 {"author": "claude", "session_id": "s", "assignments": {"copilot": []}})
                state.write_json(state.path("runs", name, "verdict.json"), {"action": "block", "blocking": [{}]})
            self.assertEqual(live.backfill(), 2)
            self.assertEqual(state.read_json(state.path("status.json"))["day"], "2020-01-02")   # stored day is old
            self.assertRegex(live.segment("claude", "s"),
                             r"^tagteam ✗ Changes requested: 1 issue to fix · \d+d ago · 0 reviews today$")
            self.assertEqual(live.backfill(), 0)                                               # once only

    def test_watch_render(self):
        live.record_run(self.done_run(author="copilot", reviewers=["agy"], action="block", blocking=1))
        live.record_run(self.done_run())
        with live.tracking(self.rec()):
            out = live.render_watch()
        self.assertIn("Running (1):", out)
        self.assertIn("Reviews today: 2", out)
        self.assertIn("copilot 1", out)
        self.assertIn("✗ Changes requested: 1 issue to fix (round 1/2)", out)

    def test_statusline_entry_reads_stdin(self):
        live.record_run(self.done_run(session_id="abc"))
        r = subprocess.run([sys.executable, "-m", "tagteam.live"], input=json.dumps({"session_id": "abc"}),
                           capture_output=True, text=True, cwd=ROOT, env={**os.environ, "NO_COLOR": "1"})
        self.assertEqual(r.stdout.strip(), "tagteam ✓ Review passed: no issues found · just now · 1 review today")
        r = subprocess.run([sys.executable, "-m", "tagteam.live", "swiftbar"], capture_output=True, text=True,
                           cwd=ROOT)
        self.assertTrue(r.stdout.startswith(":person.2.fill: :checkmark.seal.fill: 1 | "), r.stdout)
        r = subprocess.run([sys.executable, "-m", "tagteam.live"], input="not json", capture_output=True,
                           text=True, cwd=ROOT)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))


class OnReview(unittest.TestCase):
    RUN = {"ts": 1.0, "run_id": "r", "author": "claude", "session_id": "s", "tag": "turn", "round": 1, "max_rounds": 2,
           "reviewers": ["copilot"], "action": "block", "blocking": 1, "advisory": 0, "failed": [], "duration": 3.0}

    def setUp(self):
        quiet = warnings.catch_warnings()                # the detached command is never waited on, by design
        quiet.__enter__()
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(quiet.__exit__, None, None, None)

    def on_review(self, cmd):
        write_user_config(f"[hooks]\non_review = {json.dumps(cmd)}\n")

    def logged(self):
        return [r for r in state.read_jsonl(state.path("tagteam.log")) if r["msg"] == "on_review command failed"]

    def test_command_gets_the_run_on_stdin_and_outcome_in_env(self):
        out = pathlib.Path(tempfile.mkdtemp())
        self.on_review(f"cat > {out}/run.json; env | grep ^TAGTEAM_ | sort > {out}/env.tmp; mv {out}/env.tmp {out}/env")
        live.record_run(dict(self.RUN))
        for _ in range(100):
            if (out / "env").exists():
                break
            time.sleep(0.05)
        summary = "✗ Changes requested: 1 issue to fix (round 1/2)"
        self.assertEqual(json.loads((out / "run.json").read_text()), {**self.RUN, "outcome": "blocked", "summary": summary})
        env = (out / "env").read_text().splitlines()
        self.assertEqual([e for e in env if not e.startswith("TAGTEAM_HOME=")],
                         ["TAGTEAM_DEPTH=1", "TAGTEAM_MODE=block", "TAGTEAM_OUTCOME=blocked", f"TAGTEAM_SUMMARY={summary}"])

    def test_nothing_runs_when_unset_or_backfilling(self):
        with mock.patch("subprocess.Popen") as popen:
            live.record_run(dict(self.RUN))
            self.on_review("true")
            with mock.patch.object(state, "STATE", pathlib.Path(tempfile.mkdtemp())):
                state.write_json(state.path("runs", "20200101T120000-aaaa", "request.json"), {"author": "claude"})
                state.write_json(state.path("runs", "20200101T120000-aaaa", "verdict.json"), {"action": "allow"})
                self.assertEqual(live.backfill(), 1)
            popen.assert_not_called()
            live.record_run(dict(self.RUN))
            popen.assert_called_once()

    def test_an_interrupted_review_runs_it_too(self):
        now = time.time()
        rec = {"run_id": "dead", "author": "claude", "session_id": "s", "tag": "turn", "round": 1, "max_rounds": 2,
               "reviewers": ["copilot"], "started": now, "expires": now + 300}
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); from tagteam import live; "
                "cm = live.tracking(eval(sys.argv[2])); cm.__enter__(); os.kill(os.getpid(), signal.SIGKILL)")
        subprocess.run([sys.executable, "-c", code, str(ROOT), repr(rec)])
        self.on_review("true")
        with mock.patch("subprocess.Popen") as popen:
            live.active()
        self.assertEqual(popen.call_args.kwargs["env"]["TAGTEAM_OUTCOME"], "interrupted")

    def test_a_broken_command_or_config_never_fails_the_record(self):
        self.on_review("exit 3")
        live.record_run(dict(self.RUN))
        r, w = os.pipe()
        os.close(r)
        with mock.patch("subprocess.Popen", return_value=mock.Mock(stdin=os.fdopen(w, "wb"))):
            live.record_run(dict(self.RUN, run_id="r1"))         # exited without reading stdin: not a failure
        with mock.patch("subprocess.Popen", side_effect=OSError("no sh")):
            live.record_run(dict(self.RUN, run_id="r2"))
        write_user_config("[hooks]\non_review = 5\n")
        live.record_run(dict(self.RUN, run_id="r3"))
        self.assertEqual([r["run_id"] for r in state.read_jsonl(state.path("runs.jsonl"))], ["r", "r1", "r2", "r3"])
        self.assertEqual([e["error"].split("(")[0] for e in self.logged()], ["OSError", "ConfigError"])


class Encode(unittest.TestCase):
    def test_allow_notice_only_for_claude_shaped(self):
        v = {"action": "allow", "message": "[tagteam] ✓ clean"}
        for agent in ("claude", "codex"):
            self.assertEqual(json.loads(adapters.encode(agent, "turn-end", v)), {"systemMessage": "[tagteam] ✓ clean"})
        self.assertEqual(json.loads(adapters.encode("claude", "plan-gate", v)), {"systemMessage": "[tagteam] ✓ clean"})
        for agent in ("copilot", "agy"):
            self.assertEqual(adapters.encode(agent, "turn-end", v), "{}")
        self.assertEqual(adapters.encode("claude", "turn-end", None), "{}")
        self.assertEqual(json.loads(adapters.encode("claude", "turn-end", {"action": "block", "message": "m"})),
                         {"decision": "block", "reason": "m"})


if __name__ == "__main__":
    unittest.main()
