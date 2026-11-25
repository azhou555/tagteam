import json
import os
import pathlib
import re
import threading
import time
import unittest
from unittest import mock

from tests.helpers import FIX, HookRig, finding, sh, use_toml  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import adapters, ledger, live, runner, state  # noqa: E402

HELLO = "ce013625030ba8dba906f756967f9e9ca394464a"          # git hash-object of "hello\n"
FEATURE = "def f(x):\n    return x[0]\n" * 4


class Shadow(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig(use_toml(["copilot", "agy"]))

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

    def test_own_write_is_logged_and_hook_outputs_match_a_run_without_the_ledger(self):
        on = self.turn_outputs("s-on")
        line = self.shadow()
        self.assertEqual((line["today"], line["own"], line["foreign"], line["unattributed"], line["ledger_only"]),
                         ([self.p("feature.py")], [self.p("feature.py")], [], [], []))
        self.assertEqual((line["calls"], line["pending_open"], line["overlaps"]), (1, 0, 0))
        self.unrig()
        self.rig(use_toml(["copilot", "agy"]) + "[ledger]\nshadow = false\n")
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

    def test_a_worktree_subagents_write_is_ledger_only_keyed_by_the_worktree(self):
        wt = self.p(".claude/worktrees/x")
        sh(self.repo, "git", "worktree", "add", "-q", wt)
        self.prompt()
        self.write(os.path.join(wt, "wt.txt"), "hello\n", cwd=wt, agent_id="a1")
        self.stop()
        line = self.shadow()
        self.assertEqual((line["today"], line["ledger_only"]), ([], [os.path.join(wt, "wt.txt")]))
        [call] = self.ledger_calls()
        self.assertEqual((call["repo"], call["agent_id"], call["deltas"]),
                         (wt, "a1", {os.path.join(wt, "wt.txt"): [None, HELLO]}))

    def test_bash_in_a_repo_over_the_size_limit_is_skipped_and_counted(self):
        self.unrig()
        self.rig(use_toml(["copilot", "agy"]) + "[ledger]\nmax_tree_files = 0\n")
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
        with mock.patch.object(ledger, "shadow", side_effect=RuntimeError("boom")), \
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
