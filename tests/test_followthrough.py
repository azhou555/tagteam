import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import ROOT, HookRig, finding, sh, state, use_toml

from tagteam import limits, live, runner

PLAN = {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode", "session_id": "sess1"}


def verdict(*findings):
    return {"copilot": {"verdict": "request_changes" if findings else "approve", "summary": "", "findings": list(findings)}}


def issues(n):
    names = ["alpha", "bravo", "charlie"]
    return verdict(*[finding("blocker", file="feature.py", line=1 + 20 * i, claim=f"{names[i]} breaks when input is empty")
                     for i in range(n)])


def git_out(cwd, *args):
    return subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FollowThrough(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig(use_toml(["copilot"]))
        self.n = 0

    def tearDown(self):
        self.unrig()

    def start(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))

    def edit(self):
        self.n += 1
        pathlib.Path(self.repo, "feature.py").write_text(f"a = {self.n}\n" * 20)

    def stop(self, results=None, change=True, message="done", runner_fn=None):
        if change:
            self.edit()
        fake = runner_fn or self.fake_run_all(results or {})
        with mock.patch.object(runner, "run_all", fake):
            return self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=True, last_assistant_message=message))

    def plan(self, text, results=None):
        with mock.patch.object(runner, "run_all", self.fake_run_all(results or {})):
            return self.run_hook("claude", "x", {**PLAN, "cwd": self.repo, "tool_input": {"plan": text}})

    def seg(self):
        return live.segment("claude", "sess1")

    def last_prompt(self):
        return next(iter(self.calls[-1].values()))

    def test_status_shows_resolved_after_fix(self):
        self.start()
        self.assertEqual(self.stop(issues(2))["decision"], "block")
        self.assertIn("✗ Changes requested: 2 issues to fix (round 1/2)", self.seg())
        self.stop()
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())

    def test_confirm_review_after_last_block(self):
        self.start()
        self.assertEqual(self.stop(issues(2))["decision"], "block")
        self.assertEqual(self.stop(issues(2))["decision"], "block")
        self.assertIn("✗ Changes requested: 2 issues still open after fixes (round 2/2)", self.seg())
        out = self.stop(issues(1))
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("decision", out)
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", out["systemMessage"])
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", self.seg())
        self.stop(issues(1))
        self.assertEqual(len(self.calls), 3)                 # a fourth Stop spawns nothing
        self.start()                                         # a new prompt starts over; settle it this time
        self.stop(issues(2))
        self.stop(issues(2))
        self.stop()
        self.assertEqual(len(self.calls), 6)
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())
        self.stop()
        self.assertEqual(len(self.calls), 6)

    def test_confirm_review_can_fail_without_blocking(self):
        self.start()
        self.stop(issues(1))
        self.stop(issues(1))

        def failing(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            self.calls.append(jobs)
            return {r: ({"reviewer": r, "ok": False, "duration": 1}, None) for r in jobs}
        out = self.stop(runner_fn=failing)
        self.assertNotIn("decision", out)
        self.assertIn("1 earlier issue not re-checked", self.seg())

    def test_no_confirm_review_after_pass(self):
        self.start()
        self.stop(issues(1))
        self.stop()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.stop(), {})
        self.assertEqual(len(self.calls), 2)

    def small_fix(self, first="b = 0"):
        pathlib.Path(self.repo, "feature.py").write_text(f"{first}\n" + f"a = {self.n}\n" * 19)

    def session(self):
        return state.read_json(state.path("sessions", "claude-sess1.json"))

    def test_open_issues_carry_into_the_next_prompt(self):
        self.start()
        self.stop(issues(2))
        self.start()
        self.small_fix()
        out = self.stop(change=False)
        self.assertIn("copilot reviewed 2 changed lines", out["systemMessage"])   # under min_changed_lines
        self.assertIn("alpha breaks when input is empty", self.last_prompt())
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())
        self.start()
        self.small_fix("c = 0")
        self.stop(change=False)
        self.assertEqual(len(self.calls), 2)                 # resolved: nothing carries

    def test_undoing_a_carried_turn_does_not_resolve_its_issues(self):
        self.start()
        self.stop(issues(1))
        self.start()
        self.small_fix()
        self.stop(issues(1), change=False)
        self.n = 0
        self.edit()                                          # back to the file as the turn found it
        out = self.stop(change=False)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("reverted", str(out) + self.seg())
        self.assertIn("✓ Issues resolved: all 1 fixed and re-checked", self.seg())

    def test_confirm_round_closes_the_cycle(self):
        self.start()
        for _ in range(3):
            self.stop(issues(1))
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", self.seg())
        self.assertEqual(self.session()["prior"], [])
        self.start()
        self.small_fix()
        self.stop(change=False)
        self.assertEqual(len(self.calls), 3)

    def test_carried_issues_without_a_change_spawn_nothing(self):
        self.start()
        self.stop(issues(1))
        self.start()
        self.assertEqual(self.stop(change=False, message="Not a bug"), {})
        self.assertEqual(len(self.calls), 1)
        self.assertIn("✗ Changes requested: 1 issue to fix (round 1/2)", self.seg())
        self.assertEqual(len(self.session()["prior"]), 1)

    def test_killed_review_is_interrupted_and_its_issues_carry(self):
        self.start()
        self.stop(issues(2))
        self.edit()
        code = ("import io, os, signal, sys; sys.path.insert(0, sys.argv[1]); from unittest import mock; "
                "from tagteam import cli, core, harnesses, runner; "
                "mock.patch.object(core, 'headroom_map', return_value={'copilot': 0.9}).start(); "
                "mock.patch.object(harnesses, 'detected', return_value=['claude', 'copilot']).start(); "
                "mock.patch.object(runner, 'run_all', lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)).start(); "
                "sys.stdin = io.StringIO(sys.argv[2]); cli.hook('claude', 'x')")
        stop = json.dumps(self.claude("Stop", stop_hook_active=True, last_assistant_message="fixed"))
        self.assertEqual(subprocess.run([sys.executable, "-c", code, str(ROOT), stop]).returncode, -9)
        self.assertIn("⚠ Review interrupted: stopped before reviewers finished · 2 earlier issues not re-checked",
                      self.seg())
        self.seg()
        self.assertEqual([r.get("reason") for r in state.read_jsonl(state.path("runs.jsonl"))], [None, "interrupted"])
        self.start()
        self.small_fix()
        self.stop(change=False)
        self.assertIn("alpha breaks when input is empty", self.last_prompt())
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())

    def test_rebuttal_without_changes_is_rechecked(self):
        self.start()
        self.stop(issues(1))
        reply = "Not a bug: X is validated upstream"
        out = self.stop(issues(1), change=False, message=reply)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(out["decision"], "block")
        p = self.last_prompt()
        for s in (reply, "alpha breaks when input is empty", "+a = 1"):
            self.assertIn(s, p)
        again = self.stop(issues(1), change=False, message=reply)       # round 3 is the confirm round, and a repeat
        self.assertEqual(len(self.calls), 2)                            # of the same rebuttal re-issues, not re-spends
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", again["systemMessage"])

    def test_repeated_rebuttal_reissues_without_a_review(self):
        from tests.helpers import write_user_config
        write_user_config(use_toml(["copilot"]) + "[limits]\nmax_rounds_per_turn = 4\n")
        self.start()
        self.stop(issues(1))
        first = self.stop(issues(1), change=False, message="Not a bug")
        self.assertEqual((first["decision"], len(self.calls)), ("block", 2))
        self.assertEqual(self.stop(issues(1), change=False, message="Not a bug"), first)
        self.assertEqual(len(self.calls), 2)
        self.stop(issues(1), change=False, message="Still not a bug, see the validator")
        self.assertEqual(len(self.calls), 3)

    def touch_outside(self, cwd):
        path = pathlib.Path(self.outside, "notes.py")
        path.write_text("b = 1\n" * 20)
        self.run_hook("claude", "x", {**self.claude("PostToolUse", tool_name="Write", tool_input={"file_path": str(path)}),
                                      "cwd": cwd})

    def test_rebuttal_of_outside_repo_change_is_rechecked_not_reverted(self):
        self.outside = tempfile.mkdtemp()
        self.start()
        self.touch_outside(self.repo)
        self.stop(issues(1), change=False)
        self.assertEqual(len(self.calls), 1)
        out = self.stop(issues(1), change=False, message="Not a bug")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("reverted", str(out) + self.seg())
        self.assertIn("b = 1", self.last_prompt())

    def test_rebuttal_without_git_is_rechecked(self):
        self.outside = self.repo = tempfile.mkdtemp()
        self.start()
        self.touch_outside(self.outside)
        self.stop(issues(1), change=False)
        self.stop(issues(1), change=False, message="Not a bug")
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("reverted", self.seg())

    def test_commit_then_checkout_base_is_not_reverted(self):
        base = git_out(self.repo, "rev-parse", "HEAD")
        self.start()
        self.stop(issues(1))
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "work")
        sh(self.repo, "git", "checkout", "-q", "--detach", base)
        out = self.stop(issues(1), change=False)
        self.assertNotIn("reverted", str(out) + self.seg())

    def test_revert_is_not_a_round_but_a_redo_hits_the_blocked_digest(self):
        self.start()
        self.n = 0
        self.edit()
        first = self.stop(issues(1), change=False)
        self.assertEqual(first["decision"], "block")
        pathlib.Path(self.repo, "feature.py").unlink()
        self.stop(change=False)
        self.assertIn("changes reverted", self.seg())
        sf = state.path("sessions", "claude-sess1.json")
        self.assertEqual(state.read_json(sf)["round"], 1)
        self.n = 0
        self.edit()
        again = self.stop(change=False)
        self.assertEqual(again, first)                       # same code: the block is re-issued, no spawn
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state.read_json(sf)["round"], 2)
        self.assertIn("✗ Changes requested: 1 issue to fix (round 2/2)", self.seg())
        self.assertIn("1 review today", self.seg())

    def test_revert_redo_loop_cannot_land_blocked_code_as_resolved(self):
        self.start()
        sf = state.path("sessions", "claude-sess1.json")

        def redo():
            self.n = 0
            self.edit()
            return self.stop(change=False)
        self.n = 0
        self.edit()
        self.stop(issues(1), change=False)                   # 1: block
        for _ in range(2):
            pathlib.Path(self.repo, "feature.py").unlink()
            self.stop(change=False)                          # revert: resolved, nothing spawned
            self.assertIn("changes reverted", self.seg())
            last = redo()                                    # redo: block re-issued, then the confirm round
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("decision", last)
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", self.seg())
        self.assertNotIn("Issues resolved", self.seg())
        self.assertEqual(redo(), {})                         # the hook ends: no loop
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state.read_json(sf)["round"], 3)

    def test_plan_gate_silent_allow_still_resets(self):
        self.start()
        issue = finding("blocker", file="(proposed plan)", line=None, claim="step three is missing a rollback")
        self.plan("plan X\nstep\n")                          # approved
        self.plan("plan Y\nstep\n", verdict(issue))          # blocked
        self.plan("plan X\nstep\n")                          # approved digest: skipped silently, still an allow
        spawned = len(self.calls)
        self.plan("plan Z\nstep\n")
        self.assertEqual(len(self.calls), spawned + 1)
        self.assertNotIn("step three is missing a rollback", self.last_prompt())
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["round"], 1)

    def test_rebuttal_accepted_resolves(self):
        self.start()
        self.stop(issues(2))
        self.stop(change=False, message="Not a bug: validated upstream")
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())

    def test_reverted_changes_resolve_without_review(self):
        self.start()
        self.stop(issues(1))
        pathlib.Path(self.repo, "feature.py").unlink()
        out = self.stop(change=False)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("✓ Issues resolved: changes reverted", self.seg())
        self.assertIn("✓ Issues resolved: changes reverted", out["systemMessage"])
        self.assertIn("1 review today", self.seg())          # no review was spent
        self.assertEqual(live.week_spend()[0], 1)
        self.assertEqual(self.stop(change=False), {})

    def test_failed_followup_mentions_open_issues(self):
        self.start()
        self.stop(issues(2))

        def failing(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            self.calls.append(jobs)
            return {r: ({"reviewer": r, "ok": False, "duration": 1}, None) for r in jobs}
        self.stop(runner_fn=failing)
        self.assertIn("⚠ Review failed: every reviewer errored or timed out · 2 earlier issues not re-checked", self.seg())
        self.stop()                                          # the issues are still owed a re-check
        self.assertIn("✓ Issues resolved: all 2 fixed and re-checked", self.seg())

    def test_plan_gate_tracks_prior(self):
        self.start()
        plan_issue = finding("blocker", file="(proposed plan)", line=None, claim="step three is missing a rollback")
        self.plan("1. do a thing\n2. do another\n", verdict(plan_issue))
        self.assertIn("✗ Changes requested: 1 issue to fix (round 1/2)", self.seg())
        self.plan("1. do a thing\n2. do another\n3. roll back\n")
        self.assertIn("step three is missing a rollback", self.last_prompt())
        self.assertIn("✓ Issues resolved: all 1 fixed and re-checked", self.seg())

    def test_plan_gate_state_resets_after_allow(self):
        self.start()
        issue = finding("blocker", file="(proposed plan)", line=None, claim="step three is missing a rollback")
        self.plan("plan A v1\nstep\n", verdict(issue))
        self.plan("plan A v2\nstep\n")
        self.plan("plan B\nstep\n")
        p = self.last_prompt()
        self.assertNotIn("step three is missing a rollback", p)
        self.assertNotIn("Round 2", p)
        self.assertIn("✓ Review passed", self.seg())
        self.assertNotIn("Issues resolved", self.seg())
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["round"], 1)
        self.plan("plan C\nstep\n", verdict(issue))
        self.start()                                         # a new user prompt wipes the blocked plan's state
        self.plan("plan D\nstep\n")
        self.assertNotIn("step three is missing a rollback", self.last_prompt())
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["round"], 1)

    def test_plan_gate_confirm_review_cannot_block(self):
        self.start()
        issue = finding("blocker", file="(proposed plan)", line=None, claim="step three is missing a rollback")
        for i in range(2):
            self.assertEqual(self.plan(f"plan v{i}\nstep\n", verdict(issue))["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.plan("plan v3\nstep\n", verdict(issue))
        self.assertNotIn("hookSpecificOutput", out)
        self.assertEqual(len(self.calls), 3)
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", self.seg())
        self.plan("plan v4\nstep\n")                         # allowed, state reset: a fresh plan is reviewed from round 1
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["round"], 1)


class Display(unittest.TestCase):
    def run_rec(self, **kw):
        return {"ts": time.time(), "run_id": "x", "author": "claude", "session_id": "s", "tag": "turn", "round": 1,
                "max_rounds": 2, "prior": 0, "confirm": False, "reviewers": ["copilot"], "action": "allow",
                "blocking": 0, "advisory": 0, "failed": [], "duration": 3.0, **kw}

    def test_swiftbar_uses_new_kinds(self):
        live.record_run(self.run_rec(prior=2))
        head = live.render_swiftbar().splitlines()[0]
        self.assertIn(":checkmark.seal.fill:", head)
        self.assertIn(f"sfcolor2={live.KINDS['resolved']['hex']}", head)
        live.record_run(self.run_rec(confirm=True, blocking=1, prior=2))
        head = live.render_swiftbar().splitlines()[0]
        self.assertIn(":exclamationmark.triangle.fill:", head)
        self.assertIn("sfcolor2=#C46A3A,#FFC9A8", head)
        self.assertEqual(live.KINDS["unresolved"]["ansi"], 216)
        self.assertEqual(live.KINDS["resolved"]["label"], "Issues resolved")

    def test_outcome_wording(self):
        o = lambda **kw: live.summary(self.run_rec(**kw))  # noqa: E731
        self.assertEqual(o(action="block", blocking=3, prior=3, round=2), "✗ Changes requested: 3 issues still open after fixes (round 2/2)")
        self.assertEqual(o(prior=1, advisory=2), "✓ Issues resolved: all 1 fixed and re-checked, 2 minor notes")
        self.assertEqual(o(reviewers=[], reason="review limit reached (30 per hour)", prior=2),
                         "⚠ Not reviewed: review limit reached (30 per hour) · 2 earlier issues not re-checked")
        self.assertEqual(o(reviewers=[], confirm=True, blocking=2, prior=2), "⚠ Unresolved: 2 issues still open after 2 fix rounds")
        self.assertEqual(o(reviewers=[], reason="reverted", prior=2), "✓ Issues resolved: changes reverted")

    def test_unreviewed_and_reverted_runs_are_not_counted_as_reviews(self):
        live.record_run(self.run_rec(reviewers=[], reason="reverted"))
        live.record_run(self.run_rec(reviewers=[], reason="no reviewers configured"))
        self.assertIn("0 reviews today", live.segment("claude", "s"))
        self.assertIn("Reviews today: 0", live.render_watch())
        self.assertTrue(live.render_swiftbar().startswith(":person.2.fill: :exclamationmark.circle.fill: 0 | "))
        live.record_run(self.run_rec())
        self.assertIn("1 review today", live.segment("claude", "s"))


class Digest(unittest.TestCase):
    def test_rebuttal_text_cannot_masquerade_as_a_diff_part(self):
        a = {"path": "p", "diff": "d"}
        self.assertNotEqual(limits.digest([a]), limits.digest([], extra="p\0d\1"))
        self.assertEqual(limits.digest([a], ""), limits.digest([a]))
