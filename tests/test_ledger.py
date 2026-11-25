import json
import os
import pathlib
import re
import threading
import time
import unittest
from unittest import mock

from tests.helpers import FIX, HookRig, finding, sh, use_toml  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import adapters, core, ledger, live, runner, state  # noqa: E402

HELLO = "ce013625030ba8dba906f756967f9e9ca394464a"          # git hash-object of "hello\n"
FEATURE = "def f(x):\n    return x[0]\n" * 4


class Session(HookRig):
    def setUp(self):
        self.rig(use_toml(["copilot", "agy"]), attribute=True)

    def tearDown(self):
        self.unrig()

    def p(self, rel, root=None):
        return os.path.join(root or self.repo, rel)

    def prompt(self, sid="sess1"):
        return self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="go", session_id=sid))

    def tool(self, name, tool_input, act, call="toolu_1", sid="sess1", cwd=None, **kw):
        """Pre, the tool's own effect, Post: the two hook outputs."""
        payload = self.claude("PreToolUse", tool_name=name, tool_input=tool_input, tool_use_id=call, session_id=sid,
                              cwd=cwd or self.repo, **kw)
        out = [self.run_hook("claude", "x", payload)]
        act()
        out.append(self.run_hook("claude", "x", {**payload, "hook_event_name": "PostToolUse", "tool_response": {}}))
        return out

    def write(self, path, text, **kw):
        return self.tool("Write", {"file_path": path, "content": text}, lambda: pathlib.Path(path).write_text(text), **kw)

    def stop(self, sid="sess1"):
        return self.run_hook("claude", "x", self.claude("Stop", last_assistant_message="done", session_id=sid))

    def shadow(self):
        lines = state.read_jsonl(state.path("shadow.jsonl"))
        self.assertEqual(len(lines), 1)
        return lines[0]

    def ledger_calls(self, sid="sess1"):
        return state.read_json(state.path("ledger", f"claude-{sid}.json"))["calls"]

    def turn_outputs(self, sid):
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            outs = [self.prompt(sid), *self.write(self.p("feature.py"), FEATURE, sid=sid), self.stop(sid)]
        return [re.sub(r"\d+s\b", "Ns", json.dumps(o)) for o in outs]


class Shadow(Session, unittest.TestCase):

    def test_failed_bash_still_closes_its_call(self):
        self.prompt()
        payload = self.claude("PreToolUse", tool_name="Bash", tool_input={"command": "make"}, tool_use_id="t9")
        self.run_hook("claude", "x", payload)
        pathlib.Path(self.p("gen.txt")).write_text("hello\n")
        self.run_hook("claude", "x", {**payload, "hook_event_name": "PostToolUseFailure", "error": "exit 2"})
        self.stop()
        line = self.shadow()
        self.assertEqual((line["own"], line["unattributed"], line["pending_open"]), ([self.p("gen.txt")], [], 0))

    def test_a_symlink_is_logged_as_the_link_not_its_target(self):
        pathlib.Path(self.p("a.txt")).write_text("a\n")
        sh(self.repo, "git", "add", "a.txt")
        sh(self.repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a")
        self.prompt()
        self.tool("Bash", {"command": "ln -s a.txt link"}, lambda: os.symlink("a.txt", self.p("link")))
        self.stop()
        line = self.shadow()
        self.assertEqual((line["today"], line["own"], line["ledger_only"]), ([self.p("link")], [self.p("link")], []))

    def test_a_slow_snapshot_stops_bash_snapshots_in_that_repo(self):
        self.prompt()
        with mock.patch.object(ledger, "_timed_snapshot", side_effect=lambda repo: (ledger.changes.snapshot(repo), 900.0)):
            self.tool("Bash", {"command": "echo 1 > one"}, lambda: pathlib.Path(self.p("one")).write_text("1\n"), call="b1")
        self.tool("Bash", {"command": "echo 2 > two"}, lambda: pathlib.Path(self.p("two")).write_text("2\n"), call="b2")
        self.assertEqual([c["kind"] for c in self.ledger_calls()], ["tree", "skipped"])

    def test_ledger_time_at_stop_is_not_taken_from_the_review_setup_budget(self):
        self.prompt()
        self.write(self.p("feature.py"), FEATURE)
        ticks = iter([0.0, 0.0, 70.0])                   # Stop starts, the ledger starts, the ledger ends 70 s later
        deadlines = []

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            deadlines.append(deadline)
            return {r: ({"reviewer": r, "ok": True, "duration": 1}, {"verdict": "approve", "summary": "", "findings": []})
                    for r in jobs}
        from tagteam import config, core
        with mock.patch.object(core, "_clock", side_effect=lambda: next(ticks, 70.0)), \
                mock.patch.object(runner, "run_all", run_all):
            out = self.stop()
        self.assertEqual(deadlines, [config.load()["review"]["deadline_s"]])
        self.assertNotIn("too slow", json.dumps(out))

    def test_own_write_is_logged_and_hook_outputs_match_a_run_without_the_ledger(self):
        on = self.turn_outputs("s-on")
        line = self.shadow()
        self.assertEqual((line["today"], line["own"], line["foreign"], line["unattributed"], line["ledger_only"]),
                         ([self.p("feature.py")], [self.p("feature.py")], [], [], []))
        self.assertEqual((line["calls"], line["pending_open"], line["overlaps"]), (1, 0, 0))
        self.unrig()
        self.rig(use_toml(["copilot", "agy"]) + "[ledger]\nattribute = false\n", attribute=True)
        off = self.turn_outputs("s-off")
        self.assertEqual(on, off)
        self.assertIn('"systemMessage"', on[-1])                     # a review ran and reported
        self.assertEqual(len(state.read_jsonl(state.path("shadow.jsonl"))), 1)
        self.assertFalse(state.path("ledger", "claude-s-off.json").exists())

    def test_a_change_no_tool_call_made_is_unattributed(self):
        self.prompt()
        pathlib.Path(self.p("editor.py")).write_text("y = 2\n")
        self.stop()
        line = self.shadow()
        self.assertEqual((line["own"], line["unattributed"]), ([], [self.p("editor.py")]))

    def test_another_sessions_write_during_the_turn_is_foreign(self):
        self.prompt()
        self.write(self.p("b.py"), "hello\n", sid="other", call="toolu_other")
        self.stop()
        line = self.shadow()
        self.assertEqual((line["own"], line["foreign"], line["unattributed"]), ([], [self.p("b.py")], []))

    def test_bash_writing_two_files_owns_both_with_no_pre_blob(self):
        self.prompt()

        def run():
            for f in ("one.txt", "two.txt"):
                pathlib.Path(self.p(f)).write_text("hello\n")
        self.assertEqual(self.tool("Bash", {"command": "echo hello | tee one.txt two.txt"}, run), [{}, {}])
        self.stop()
        line = self.shadow()
        self.assertEqual(line["own"], [self.p("one.txt"), self.p("two.txt")])
        [call] = self.ledger_calls()
        self.assertEqual((call["kind"], call["deltas"]),
                         ("tree", {self.p("one.txt"): [None, HELLO], self.p("two.txt"): [None, HELLO]}))
        self.assertEqual(len(line["bash_snapshot_ms"]), 1)

    def test_a_worktree_subagents_write_is_keyed_by_the_worktree_and_reviewed(self):
        wt = self.p(".claude/worktrees/x")
        sh(self.repo, "git", "worktree", "add", "-q", wt)
        self.prompt()
        self.write(os.path.join(wt, "wt.txt"), "hello\n" * 10, cwd=wt, agent_id="a1")
        bad = {"verdict": "changes_requested", "summary": "",
               "findings": [finding(sev="blocker", file=".claude/worktrees/x/wt.txt", line=2)]}
        with mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": bad})):
            out = self.stop()
        self.assertEqual(out["decision"], "block")
        self.assertIn("+hello", self.calls[0]["copilot"])
        line = self.shadow()
        self.assertEqual((line["today"], line["ledger_only"]), ([], [os.path.join(wt, "wt.txt")]))
        [call] = self.ledger_calls()
        self.assertEqual((call["repo"], call["agent_id"], call["deltas"]),
                         (wt, "a1", {os.path.join(wt, "wt.txt"): [None, ledger.changes.blob_ids(
                             wt, [os.path.join(wt, "wt.txt")])[os.path.join(wt, "wt.txt")]]}))

    def test_bash_in_a_repo_over_the_size_limit_is_skipped_and_counted(self):
        self.unrig()
        self.rig(use_toml(["copilot", "agy"]) + "[ledger]\nmax_tree_files = 0\n", attribute=True)
        self.prompt()
        self.tool("Bash", {"command": "true"}, lambda: None)
        self.stop()
        line = self.shadow()
        self.assertEqual((line["calls"], line["skipped"], line["bash_snapshot_ms"]), (1, 1, []))

    def test_a_ledger_failure_never_changes_a_hook(self):
        self.prompt()
        with mock.patch.object(ledger, "pre", side_effect=RuntimeError("boom")):
            outs = self.write(self.p("feature.py"), FEATURE)
        bad = {"verdict": "changes_requested", "summary": "", "findings": [finding(file="feature.py", line=2)]}
        with mock.patch.object(ledger, "owners", side_effect=RuntimeError("boom")), \
                mock.patch.object(runner, "run_all", self.fake_run_all({"copilot": bad, "agy": bad})):
            out = self.stop()
        self.assertEqual(outs, [{}, {}])
        self.assertEqual(out["decision"], "block")
        msgs = [r["msg"] for r in state.read_jsonl(state.path("tagteam.log"))]
        self.assertEqual((msgs.count("ledger error"), msgs.count("hook error")), (2, 0))
        self.assertEqual(live._load_status(time.time())["errors"], [])

    def test_entries_older_than_a_day_are_pruned_on_the_next_write(self):
        f = state.path("ledger", "claude-sess1.json")
        old = time.time() - 2 * 86400
        state.write_json(f, {"pending": {"stale": {"t0": old, "kind": "files", "pre": {}}},
                             "calls": [{"call": "c0", "t0": old, "t1": old, "deltas": {}}]})
        self.write(self.p("b.py"), "hello\n")
        d = state.read_json(f)
        self.assertEqual((d["pending"], [c["call"] for c in d["calls"]]), ({}, ["toolu_1"]))


BLOCKER = lambda file: {"verdict": "changes_requested", "summary": "",          # noqa: E731
                        "findings": [finding(sev="blocker", file=file, line=2, claim=f"bug in {file}")]}


class Attribution(Session, unittest.TestCase):
    """Only files this session's own tool calls changed can block; every other change is still reviewed."""

    def stop_with(self, results, sid="sess1"):
        with mock.patch.object(runner, "run_all", self.fake_run_all(results)):
            return self.stop(sid)

    def last_run(self):
        return state.read_jsonl(state.path("runs.jsonl"))[-1]

    def test_a_blocker_in_an_own_file_blocks(self):
        self.prompt()
        self.write(self.p("mine.py"), FEATURE)
        out = self.stop_with({"copilot": BLOCKER("mine.py")})
        self.assertEqual(out["decision"], "block")
        self.assertIn("1. [blocker] mine.py:2: bug in mine.py  (copilot)\n", out["reason"])

    def test_another_sessions_change_is_reviewed_in_the_background_and_never_blocks(self):
        self.prompt()
        self.write(self.p("theirs.py"), FEATURE, sid="other", call="toolu_other")
        out = self.stop_with({"copilot": BLOCKER("theirs.py")})
        self.assertEqual(out, {"systemMessage": core.BACKGROUND})
        run = self.last_run()
        self.assertEqual((run["mode"], run["action"], run["blocking"], run["suggestions"]), ("advise", "allow", 0, 1))
        verdict = state.read_json(state.path("runs", run["run_id"], "verdict.json"))
        self.assertIn("[not blocking: this file changed outside your own tool calls]", verdict["message"])

    def test_in_a_mixed_run_only_the_own_files_finding_blocks(self):
        self.prompt()
        self.write(self.p("mine.py"), FEATURE)
        pathlib.Path(self.p("editor.py")).write_text(FEATURE)
        both = {"verdict": "changes_requested", "summary": "", "findings": [
            finding(sev="blocker", file="mine.py", line=2, claim="mine is broken"),
            finding(sev="blocker", file="editor.py", line=2, claim="editor is broken"),
            finding(sev="blocker", file="", line=None, claim="general concern")]}
        out = self.stop_with({"copilot": both})
        self.assertEqual(out["decision"], "block")
        blocking, suggestions = out["reason"].split("Suggestions (not blocking")
        self.assertIn("mine is broken", blocking)
        self.assertNotIn("editor is broken", blocking)
        self.assertIn("editor.py:2: editor is broken", suggestions)
        self.assertIn("general concern", suggestions)

    def test_an_edit_by_someone_else_after_the_sessions_own_breaks_its_claim(self):
        self.prompt()
        self.write(self.p("f.py"), FEATURE)
        pathlib.Path(self.p("f.py")).write_text(FEATURE + "x = 1\n")
        self.assertEqual(self.stop_with({"copilot": BLOCKER("f.py")}), {"systemMessage": core.BACKGROUND})
        self.assertEqual(self.shadow()["unattributed"], [self.p("f.py")])

    def test_parallel_subagents_writing_one_file_still_own_it(self):
        self.prompt()
        path = self.p("shared.py")
        pre = lambda call, agent: self.claude("PreToolUse", tool_name="Write", tool_use_id=call, agent_id=agent,  # noqa: E731
                                              tool_input={"file_path": path, "content": ""})
        post = lambda payload: {**payload, "hook_event_name": "PostToolUse", "tool_response": {}}  # noqa: E731
        a, b = pre("ta", "a1"), pre("tb", "a2")
        self.run_hook("claude", "x", a)
        self.run_hook("claude", "x", b)
        pathlib.Path(path).write_text(FEATURE)
        pathlib.Path(path).write_text(FEATURE + FEATURE)
        self.run_hook("claude", "x", post(a))
        self.run_hook("claude", "x", post(b))
        out = self.stop_with({"copilot": BLOCKER("shared.py")})
        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.shadow()["overlaps"], 1)

    def test_a_subagent_write_after_stop_is_reviewed_at_the_next_stop(self):
        self.prompt()
        self.write(self.p("first.py"), FEATURE)
        self.stop_with({})
        self.write(self.p("late.py"), FEATURE, call="toolu_late", agent_id="bg1")   # a background subagent, after Stop
        self.prompt()                                    # the <task-notification> turn
        out = self.stop_with({"copilot": BLOCKER("late.py")})
        self.assertEqual(out["decision"], "block")
        self.assertIn("late.py", self.calls[-1]["copilot"])
        self.assertNotIn("first.py", self.calls[-1]["copilot"])

    def test_a_change_between_turns_with_no_own_work_is_not_reviewed(self):
        self.prompt()
        self.write(self.p("first.py"), FEATURE)
        self.stop_with({})
        pathlib.Path(self.p("editor.py")).write_text(FEATURE)
        self.prompt()
        self.assertEqual(self.stop_with({}), {})
        self.assertEqual(len(self.calls), 1)

    def test_a_background_result_after_the_next_turn_began_closes_what_it_reviewed(self):
        self.unrig()
        self.rig(use_toml(["copilot"]), small_blocks=False, attribute=True)
        self.run_detached = False
        self.prompt()
        self.write(self.p("bg.py"), FEATURE)
        self.assertEqual(self.stop_with({}), {"systemMessage": core.BACKGROUND})
        self.prompt()                                    # own calls since the baseline: this turn keeps it
        with mock.patch.object(runner, "run_all", self.fake_run_all({})):
            core.background(self.detached[0])
        self.assertEqual(self.stop_with({}), {})        # nothing new since the background review's tree
        self.assertEqual(len(self.calls), 1)

    def test_hooks_older_than_the_package_review_everything_as_own(self):
        self.prompt()
        pathlib.Path(self.p("mine.py")).write_text(FEATURE)
        self.run_hook("claude", "x", self.claude("PostToolUse", tool_name="Write", tool_use_id="nopre",
                                                 tool_input={"file_path": self.p("mine.py")}, tool_response={}))
        out = self.stop_with({"copilot": BLOCKER("mine.py")})
        self.assertEqual(out["decision"], "block")
        self.assertTrue(self.shadow()["fallback"])

    def test_own_calls_pruned_after_a_day_leave_their_change_unattributed(self):
        self.prompt()
        self.write(self.p("old.py"), FEATURE)
        old = time.time() - 2 * 86400
        f, sf = state.path("ledger", "claude-sess1.json"), state.path("sessions", "claude-sess1.json")
        state.write_json(f, {**state.read_json(f), "calls": [{**c, "t0": old, "t1": old} for c in self.ledger_calls()]})
        state.write_json(sf, {**state.read_json(sf), "baseline_ts": old - 1})
        self.write(self.p("new.py"), FEATURE, call="toolu_2")   # this write prunes the day-old call
        self.stop_with({"copilot": BLOCKER("old.py")})
        line = self.shadow()
        self.assertEqual((line["own"], line["unattributed"]), ([self.p("new.py")], [self.p("old.py")]))
        self.assertEqual(self.last_run()["blocking"], 0)

    def test_a_finding_naming_no_file_blocks_when_every_file_is_own(self):
        self.prompt()
        self.write(self.p("mine.py"), FEATURE)
        out = self.stop_with({"copilot": {"verdict": "changes_requested", "summary": "",
                                          "findings": [finding(sev="blocker", file="", line=None, claim="general")]}})
        self.assertEqual(out["decision"], "block")

    def test_another_sessions_write_inside_an_own_bash_call_is_contested(self):
        self.prompt()

        def other_writes():
            self.write(self.p("theirs.py"), FEATURE, sid="other", call="toolu_other")
        self.tool("Bash", {"command": "sleep 1"}, other_writes, call="bash1")
        self.assertEqual(self.stop_with({"copilot": BLOCKER("theirs.py")}), {"systemMessage": core.BACKGROUND})
        self.assertEqual(self.shadow()["foreign"], [self.p("theirs.py")])

    def worktree_blocked(self):
        wt = self.p(".claude/worktrees/x")
        sh(self.repo, "git", "worktree", "add", "-q", wt)
        self.prompt()
        self.write(os.path.join(wt, "empty.txt"), "", cwd=wt, agent_id="a1", call="t0")
        self.write(os.path.join(wt, "wt.py"), FEATURE, cwd=wt, agent_id="a1")
        out = self.stop_with({"copilot": BLOCKER(".claude/worktrees/x/wt.py")})
        self.assertEqual(out["decision"], "block")
        return wt

    def test_an_empty_worktree_file_does_not_hide_the_worktrees_other_changes(self):
        self.worktree_blocked()
        self.assertIn("wt.py", self.calls[0]["copilot"])

    def test_an_unchanged_stop_after_a_worktree_blocker_rechecks_instead_of_resolving(self):
        self.worktree_blocked()
        out = self.stop_with({"copilot": BLOCKER(".claude/worktrees/x/wt.py")})
        self.assertEqual(out["decision"], "block")
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(self.last_run().get("reason"), "reverted")

    def test_a_partial_worktree_fix_is_reviewed(self):
        wt = self.worktree_blocked()
        self.write(os.path.join(wt, "wt.py"), FEATURE + "y = 1\n", cwd=wt, agent_id="a1", call="fix")
        self.stop_with({})
        self.assertEqual(len(self.calls), 2)
        self.assertIn("+y = 1", self.calls[1]["copilot"])

    def test_another_sessions_write_inside_an_own_bash_call_in_a_worktree_is_contested(self):
        wt = self.p(".claude/worktrees/x")
        sh(self.repo, "git", "worktree", "add", "-q", wt)
        self.prompt()

        def other_writes():
            self.write(os.path.join(wt, "theirs.py"), FEATURE, sid="other", call="toolu_other", cwd=wt)
        self.tool("Bash", {"command": "sleep 1"}, other_writes, call="bash1", cwd=wt, agent_id="a1")
        self.assertEqual(self.stop_with({"copilot": BLOCKER(".claude/worktrees/x/theirs.py")}),
                         {"systemMessage": core.BACKGROUND})


class Journal(unittest.TestCase):
    def test_an_append_racing_a_prune_is_never_lost(self):
        repo, old = "/r", time.time() - 2 * 86400
        p = ledger._journal(repo)
        p.write_text(json.dumps({"ts": old, "pad": "x" * ledger.JOURNAL_MAX}) + "\n")
        read, racer = state.read_jsonl, []

        def read_then_race(*a):
            out = read(*a)
            if not racer:                            # another session appends after the prune has read
                racer.append(threading.Thread(target=ledger.journal_append, args=(repo, {"ts": time.time(), "n": 2})))
                racer[0].start()
                racer[0].join(0.3)
            return out
        with mock.patch.object(state, "read_jsonl", read_then_race):
            ledger.journal_append(repo, {"ts": time.time(), "n": 1})
        racer[0].join()
        self.assertEqual([r.get("n") for r in read(p)], [1, 2])


class Parse(unittest.TestCase):
    def test_captured_subagent_calls_pair_pre_and_post_by_call_id(self):
        sides = {}
        for line in (FIX / "subagents" / "claude-subagents.jsonl").read_text().splitlines():
            p = json.loads(line)
            if p["hook_event_name"] in ("PreToolUse", "PostToolUse") and p.get("tool_name") in ("Write", "Bash"):
                ev = adapters.parse("claude", "x", p)
                sides.setdefault(ev.call["id"], []).append(ev.phase)
        self.assertGreater(len(sides), 4)
        self.assertEqual({tuple(v) for v in sides.values()}, {("pre", "touch")})
