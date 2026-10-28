import contextlib
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

from tests.helpers import CFG, REAL_DETACH, ROOT, HookRig, finding, sh, state, use_toml, write_user_config

from tagteam import adapters, cli, core, limits, live, quota, runner, sandbox, verdict

BUG = finding(file="feature.py", line=1, claim="the loop skips the last item")


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
        self.rig(use_toml(["copilot"]))

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
        write_user_config("review.deadline_s = 1\n" + use_toml(["copilot"]))
        self.hold_lock(3)
        out = self.stop()
        self.assertEqual((out["systemMessage"], self.calls), ("[tagteam] ⚠ Not reviewed: earlier review still running", []))
        self.assertIn("⚠ Not reviewed: earlier review still running", self.seg())
        time.sleep(2.2)
        write_user_config(use_toml(["copilot"]))
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


class Background(HookRig, unittest.TestCase):
    """Small changes don't block by default: the review runs detached and its findings arrive later."""

    def setUp(self):
        self.rig(use_toml(["copilot"]), small_blocks=False)
        quiet = warnings.catch_warnings()                # a detached review is never waited on, by design
        quiet.__enter__()
        warnings.simplefilter("ignore", ResourceWarning)
        self.addCleanup(quiet.__exit__, None, None, None)
        self.n = 0

    def tearDown(self):
        self.unrig()

    def event(self, agent, name, **kw):
        codex = {"transcript_path": "/Users/u/.codex/sessions/x.jsonl"} if agent == "codex" else {}
        return self.run_hook(agent, "x", self.claude(name, **kw, **codex))

    def start(self, prompt="add feature", agent="claude"):
        return self.event(agent, "UserPromptSubmit", prompt=prompt)

    def stop(self, *findings, lines=20, change=True, runner_fn=None, name="feature.py", agent="claude"):
        if change:
            self.n += 1
            pathlib.Path(self.repo, name).write_text(f"a = {self.n}\n" * lines)
        fake = runner_fn or self.fake_run_all({"copilot": {"verdict": "approve", "summary": "", "findings": list(findings)}})
        with mock.patch.object(runner, "run_all", fake):
            return self.event(agent, "Stop", last_assistant_message="done")

    def session(self):
        return state.read_json(state.path("sessions", "claude-sess1.json"))

    def open_issues(self):
        return [(f["claim"], f["severity"], f["gate"]) for f in self.session()["prior"]]

    def seg(self):
        return live.segment("claude", "sess1")

    def test_a_small_change_ends_the_turn_at_once_and_its_findings_become_suggestions(self):
        self.start()
        self.assertEqual(self.stop(BUG), {"systemMessage": core.BACKGROUND})
        self.assertEqual(core.BACKGROUND, "[tagteam] ⟳ Reviewing in the background; results appear in the status line")
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "major", "advise")])
        self.assertIn("tagteam ● Suggestions: 1 issue, not blocking · just now", self.seg())
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((run["mode"], run["action"], run["blocking"], run["suggestions"]), ("advise", "allow", 0, 1))
        self.assertEqual((self.session()["round"], self.session()["owed"]), (0, None))   # no round spent

    def test_without_a_status_line_the_result_shows_once_at_the_next_prompt(self):
        self.start(agent="codex")
        self.assertEqual(self.stop(agent="codex"), {"systemMessage": core.BACKGROUND_NEXT})
        shown = self.start(agent="codex")
        self.assertEqual(list(shown), ["systemMessage"])
        self.assertTrue(shown["systemMessage"].startswith("[tagteam] ✓ Review passed: no issues found · copilot reviewed"))
        self.assertEqual(self.start(agent="codex"), {})                      # once
        self.start(agent="codex")
        self.stop(BUG, agent="codex")
        shown = self.start(agent="codex")                                     # suggestions: the model and the user
        self.assertIn("Open suggestions from an earlier cross-review", shown["hookSpecificOutput"]["additionalContext"])
        self.assertIn("● Suggestions: 1 issue, not blocking", shown["systemMessage"])

    def test_a_background_blocker_blocks_the_next_stop_instead_of_a_notice(self):
        self.start(agent="codex")
        self.stop({**BUG, "severity": "blocker"}, agent="codex")
        self.assertNotIn("systemMessage", self.start(agent="codex"))

    def test_claude_keeps_the_status_line_and_gets_no_prompt_notice(self):
        self.start()
        self.stop()
        self.assertEqual(self.start(), {})

    def test_a_suggestion_is_rechecked_only_by_a_review_of_its_own_file(self):
        self.start()
        self.stop(BUG)
        self.start()
        self.stop(name="other.py")                           # unrelated change, reviewer reports nothing
        self.assertEqual(("skips the last item" in next(iter(self.calls[-1].values())), self.open_issues()),
                         (False, [("the loop skips the last item", "major", "advise")]))
        self.start()
        self.stop()                                          # feature.py changed and nothing re-reported: fixed
        self.assertEqual(("skips the last item" in next(iter(self.calls[-1].values())), self.open_issues()),
                         (True, []))

    def test_the_confirm_round_closes_the_blocking_cycle_not_an_untouched_suggestion(self):
        self.start()
        self.stop(BUG)
        self.start()
        other = finding(file="other.py", line=1, claim="the cache never expires")
        for i, want in enumerate(["block", "block", None]):          # rounds 1 and 2 block; round 3 is the confirm
            self.assertEqual(self.stop(other, lines=200 + i, name="other.py").get("decision"), want)
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "major", "advise")])

    def test_no_background_run_when_it_could_not_review_or_start(self):
        self.start()
        write_user_config(use_toml([]))
        self.assertEqual(self.stop(BUG), {"systemMessage": "[tagteam] ⚠ Not reviewed: no reviewers configured; run tagteam install"})
        write_user_config(use_toml(["copilot"]))
        with mock.patch.object(core, "_detach", side_effect=OSError("fork failed")):
            out = self.stop(BUG)
        self.assertEqual(out["systemMessage"].split(" · ")[0], "[tagteam] ● Suggestions: 1 issue, not blocking")
        self.assertEqual((len(self.calls), list(state.path("jobs", "x").parent.iterdir())), (1, []))

    def test_the_next_prompt_gets_open_suggestions_as_context(self):
        self.start()
        self.stop(BUG)
        context = ("[tagteam] Open suggestions from an earlier cross-review, not blocking; fix when convenient:\n"
                   "1. [major] feature.py:1: the loop skips the last item  (copilot)\n   Evidence: e\n   Fix: f")
        self.assertEqual(self.start("next"), {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                                     "additionalContext": context}})
        sent = {"action": "context", "message": context}
        self.assertEqual({a: json.loads(adapters.encode(a, "turn-start", sent)) for a in ("codex", "copilot", "agy")},
                         {"codex": {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
                          "copilot": {}, "agy": {}})
        self.stop(lines=19)                                  # fixed: re-checked in the background, nothing left
        self.assertEqual(self.start("after"), {})

    def test_the_plan_gate_always_blocks(self):
        write_user_config("review.blocking.docs = false\n" + use_toml(["copilot"]))
        self.start()
        issue = finding("major", file="(proposed plan)", line=None, claim="step three has no rollback")
        with mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": {"findings": [issue]}})):
            out = self.run_hook("claude", "x", self.claude("PreToolUse", tool_name="ExitPlanMode",
                                                           tool_input={"plan": "1. a\n2. b\n"}))
        self.assertEqual((out["hookSpecificOutput"]["permissionDecision"], self.detached), ("deny", []))

    def test_a_running_background_review_neither_delays_nor_skips_the_plan_gate(self):
        write_user_config(use_toml(["copilot"]))
        self.start()
        issue = finding("major", file="(proposed plan)", line=None, claim="step three has no rollback")
        with limits.session_lock("claude", "sess1") as (lock, _), \
                mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": {"findings": [issue]}})):
            t0 = time.monotonic()
            out = self.run_hook("claude", "x", self.claude("PreToolUse", tool_name="ExitPlanMode",
                                                           tool_input={"plan": "1. a\n2. b\n"}))
            waited = time.monotonic() - t0
        self.assertEqual((lock, out["hookSpecificOutput"]["permissionDecision"], waited < 5), ("held", "deny", True))

    def test_a_large_change_still_blocks(self):
        self.start()
        out = self.stop(BUG, lines=200)
        self.assertEqual((out["decision"], self.detached), ("block", []))
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "major", "block")])

    def test_a_rechecked_suggestion_does_not_block_a_large_change_until_it_is_a_blocker(self):
        self.start()
        self.stop(BUG)
        self.start()
        out = self.stop(BUG, lines=200)
        self.assertEqual((out.get("decision"), self.detached[1:]), (None, []))     # a blocking review, and it allowed
        self.assertIn("● Suggestions: 1 issue, not blocking", out["systemMessage"])
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "major", "advise")])
        self.start()
        out = self.stop({**BUG, "severity": "blocker"}, lines=200)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "blocker", "block")])

    def test_a_blocker_from_a_background_review_blocks_the_next_stop(self):
        self.start()
        self.assertEqual(self.stop({**BUG, "severity": "blocker"}), {"systemMessage": core.BACKGROUND})
        self.assertIn("✗ Changes requested: 1 issue to fix (round 1/2)", self.seg())
        self.start("something else")
        out = self.stop(change=False)
        self.assertEqual(out["decision"], "block")
        self.assertIn("1. [blocker] feature.py:1: the loop skips the last item  (copilot)", out["reason"])
        self.assertEqual((len(self.calls), self.session()["round"], self.session()["owed"]), (1, 1, None))
        self.assertIn("✗ Changes requested: 1 issue to fix (round 1/2)", self.seg())
        out = self.stop()                                    # the fix: a blocking round 2, not a background one
        self.assertEqual((out.get("decision"), len(self.calls), len(self.detached)), (None, 2, 1))
        self.assertIn("the loop skips the last item", next(iter(self.calls[-1].values())))
        self.assertIn("✓ Issues resolved: all 1 fixed and re-checked", self.seg())

    def test_a_review_finishing_after_the_next_turn_started_lands_in_that_turn(self):
        self.run_detached = False
        self.start()
        self.stop({**BUG, "severity": "blocker"})
        self.start("next request")
        before = self.session()
        with mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": {"findings": [{**BUG, "severity": "blocker"}]}})):
            core.background(self.detached[-1])
        after = self.session()
        rest = lambda d: {k: v for k, v in d.items() if k not in ("prior", "carried", "owed")}  # noqa: E731
        self.assertEqual(rest(after), rest(before))
        self.assertEqual((after["prompt"], after["round"], after["carried"]), ("next request", 0, True))
        self.assertEqual(self.open_issues(), [("the loop skips the last item", "blocker", "block")])
        self.assertIn("the loop skips the last item", after["owed"])
        self.assertEqual(self.stop(change=False)["decision"], "block")
        self.assertFalse(pathlib.Path(self.detached[-1]).exists())

    def test_the_author_working_on_during_a_background_review_disables_nobody(self):
        self.run_detached = False
        self.start()
        self.stop()
        self.start("next request")
        real, sandbox_saw = self.fake_run_all({}), []

        def busy_author(jobs, cfg, cwd, *a, **kw):
            work = pathlib.Path(cwd["copilot"])
            sandbox_saw.append((work.joinpath("feature.py").read_text()[:5], work.joinpath("next.py").exists()))
            pathlib.Path(self.repo, "feature.py").write_text("changed again\n")
            sh(self.repo, "git", "add", "-A")
            sh(self.repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "next")
            return real(jobs, cfg, cwd, *a, **kw)
        pathlib.Path(self.repo, "next.py").write_text("b = 2\n" * 20)    # before the review starts
        with mock.patch.object(runner, "run_all", busy_author):
            core.background(self.detached[-1])
        run = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((limits.disabled(), "real_repo_changed" in run, run["mode"]), ({}, False, "advise"))
        self.assertEqual(sandbox_saw, [("a = 1", False)])    # the tree the turn ended with, not later edits

    def test_a_background_review_with_no_sandbox_does_not_run(self):
        self.start()
        with mock.patch.object(sandbox, "_worktree", side_effect=OSError("no worktrees here")):
            self.assertEqual(self.stop(BUG), {"systemMessage": core.BACKGROUND})
        self.assertEqual(self.calls, [])
        self.assertIn("⚠ Not reviewed: no sandbox for a background review, see tagteam.log", self.seg())

    def test_a_killed_background_review_is_recorded_interrupted(self):
        self.run_detached = False
        self.start()
        self.stop()
        code = ("import os, signal, sys; sys.path.insert(0, sys.argv[1]); from unittest import mock; "
                "from tagteam import core, harnesses, runner; "
                "mock.patch.object(harnesses, 'detected', return_value=['claude', 'copilot']).start(); "
                "mock.patch.object(runner, 'run_all', lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)).start(); "
                "core.background(sys.argv[2])")
        self.assertEqual(subprocess.run([sys.executable, "-c", code, str(ROOT), self.detached[-1]]).returncode, -9)
        self.assertIn("⚠ Review interrupted: stopped before reviewers finished", self.seg())
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["mode"], "advise")

    def test_the_detached_child_keeps_the_session_lock_until_it_exits(self):
        with limits.session_lock("claude", "sess1") as (lock, fd):
            REAL_DETACH([sys.executable, "-c", "import time; time.sleep(1.5)"], fd)
        with limits.session_lock("claude", "sess1") as (lock, _):
            self.assertEqual(lock, limits.BUSY)                # the hook is gone; its child still holds the lock
        with limits.session_lock("claude", "sess1", wait=10) as (lock, _):
            self.assertEqual(lock, limits.HELD)

    def test_a_real_detached_review_holds_the_session_until_it_writes_back(self):
        """End to end through `python -m tagteam background`, with a slow stand-in copilot CLI on PATH."""
        bin_dir = tempfile.mkdtemp()
        out = json.dumps({"verdict": "changes_requested", "summary": "", "findings": [BUG]})
        pathlib.Path(bin_dir, "copilot").write_text(f"#!/bin/sh\nsleep 2\ncat <<'EOF'\n{out}\nEOF\n")
        os.chmod(pathlib.Path(bin_dir, "copilot"), 0o755)
        self.start()
        with mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]}), \
                mock.patch.object(core, "_detach", REAL_DETACH):
            self.assertEqual(self.stop(), {"systemMessage": core.BACKGROUND})
            self.start("next")
            self.assertEqual(self.session()["prior"], [])            # the child is still reviewing
            self.stop(lines=200)                                      # waits for it, then reviews in the foreground
        self.assertIn("the loop skips the last item", next(iter(self.calls[-1].values())))
        runs = state.read_jsonl(state.path("runs.jsonl"))
        self.assertEqual([(r["mode"], r["reviewers"], r["suggestions"]) for r in runs],
                         [("advise", ["copilot"], 1), ("block", ["copilot"], 0)])
        self.assertEqual(self.session()["prior"], [])


class Surfaces(unittest.TestCase):
    """Every surface tells a review the author waits for from one it doesn't."""

    def run_rec(self, **kw):
        return {"ts": time.time(), "run_id": "x", "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
                "max_rounds": 2, "prior": 0, "confirm": False, "mode": "advise", "reviewers": ["copilot"],
                "action": "allow", "blocking": 0, "suggestions": 2, "advisory": 0, "failed": [], "duration": 3.0, **kw}

    def running(self, **kw):
        now = time.time()
        return live.tracking({"run_id": "r1", "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
                              "max_rounds": 2, "reviewers": ["copilot"], "started": now, "expires": now + 300, **kw})

    def test_running(self):
        with self.running(mode="advise", background=True):
            self.assertIn("tagteam ⟳ Reviewing in background · copilot … 0s", live.segment("claude", "s"))
            self.assertIn("  ⟳ Reviewing in background  claude  copilot … 0s", live.render_watch())
            self.assertIn("⟳ Reviewing in background · :circle.fill: claude → :circle.fill: copilot | color=",
                          live.render_swiftbar())
        with self.running():
            self.assertIn("tagteam ⟳ Reviewing changes, round 1/2 · copilot … 0s", live.segment("claude", "s"))
            self.assertIn("⟳ Reviewing changes, round 1/2 · :circle.fill: claude → :circle.fill: copilot | color=",
                          live.render_swiftbar())

    def test_findings(self):
        self.assertEqual(live.summary(self.run_rec()), "● Suggestions: 2 issues, not blocking")
        self.assertEqual(live.summary(self.run_rec(suggestions=1, advisory=1, prior=3)),
                         "● Suggestions: 1 issue, not blocking, 1 minor note")
        self.assertEqual(live.summary(self.run_rec(mode="block", action="block", blocking=1)),
                         "✗ Changes requested: 1 issue to fix (round 1/2)")
        self.assertEqual((live.KINDS["suggestions"]["hex"], live.KINDS["blocked"]["hex"]), ("#3B7DC4,#B3D4FF", "#C8554F,#FFB3B3"))
        live.record_run(self.run_rec())
        self.assertIn("tagteam\x1b[0m \x1b[38;5;153m● Suggestions: 2 issues, not blocking\x1b[0m", live.segment("claude", "s", color=True))
        self.assertIn("● Suggestions: 2 issues, not blocking  (not blocking, 3s)", live.render_watch())
        self.assertIn("● Suggestions: 2 issues, not blocking · not blocking | color=#3B7DC4,#B3D4FF", live.render_swiftbar())
        self.assertIn("sfcolor2=#3B7DC4,#B3D4FF", live.render_swiftbar().splitlines()[0])

    def test_each_agent_has_its_color(self):
        live.record_run({**self.run_rec(), "reviewers": ["codex", "copilot"]})
        plain, colored = live.render_watch(), live.render_watch(color=True)
        self.assertEqual(re.sub(r"\x1b\[[0-9;]*m", "", colored), plain)     # colors don't shift the columns
        recent = next(ln for ln in colored.splitlines() if " -> " in ln)
        for name in ("claude", "codex", "copilot"):
            self.assertIn(f"\x1b[38;5;{live.AGENTS[name]['ansi']}m{name}", recent)
        row = next(ln for ln in live.render_swiftbar().splitlines() if "→" in ln)
        self.assertIn(":circle.fill: claude → :circle.fill: codex, :circle.fill: copilot: ", row)
        dots = enumerate(["claude", "codex", "copilot"], 1)
        self.assertIn(" ".join(f"sfcolor{i}={live.AGENTS[n]['hex']}" for i, n in dots), row)

    def test_menu_bar_groups_open_issues(self):
        state.write_json(state.path("sessions", "claude-s.json"), {"prior": [
            {**finding("minor", file="b.py", claim="name is unclear"), "gate": "advise"},
            {**finding("blocker", file="a.py", claim="drops the last row"), "gate": "block"}]})
        out = live.render_swiftbar().splitlines()
        i = out.index("Open issues · Blocking | size=11 color=gray")
        self.assertEqual(out[i:i + 4], [
            "Open issues · Blocking | size=11 color=gray", "[blocker] a.py: drops the last row (claude) | color=#C8554F,#FFB3B3",
            "Open issues · Not blocking | size=11 color=gray", "[minor] b.py: name is unclear (claude) | color=#3B7DC4,#B3D4FF"])

    def test_status_command(self):
        live.record_run(self.run_rec())
        out = io.StringIO()
        with self.running(mode="advise", background=True), mock.patch.object(quota, "headroom", return_value=None), \
                contextlib.redirect_stdout(out):
            cli.status(7)
        self.assertRegex(out.getvalue(), r"r1  claude  -> copilot  \d+s  in background")
        self.assertIn("● Suggestions: 2 issues, not blocking  (not blocking)", out.getvalue())

    def test_on_review_gets_the_mode(self):
        write_user_config('[hooks]\non_review = "true"\n')
        with mock.patch("subprocess.Popen") as popen:
            live.record_run(self.run_rec())
        self.assertEqual(popen.call_args.kwargs["env"]["TAGTEAM_MODE"], "advise")


if __name__ == "__main__":
    unittest.main()
