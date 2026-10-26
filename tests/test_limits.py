import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import CFG, HookRig, finding, make_repo, state, use_toml, write_user_config  # noqa: F401

from tagteam import limits, quota, runner

ROOT = pathlib.Path(__file__).resolve().parent.parent
RES = state.path("reservations.jsonl")
NOTICE = "[tagteam] ⚠ Not reviewed: review limit reached ({n} per hour)"


def cfg_max(n):
    return {**CFG, "limits": {**CFG["limits"], "max_reviews_per_hour": n}}


def seed(n, age=0.0):
    now = time.time()
    RES.write_text("".join(json.dumps({"run_id": f"r{i}", "session": "x", "ts": now - age}) + "\n" for i in range(n)))


class Reservations(unittest.TestCase):
    def setUp(self):
        RES.unlink(missing_ok=True)

    def test_default_ceiling_is_30(self):
        self.assertEqual(CFG["limits"]["max_reviews_per_hour"], 30)

    def test_parallel_reservations_never_exceed_ceiling(self):
        seed(4)
        env = {**os.environ, "TAGTEAM_HOME": str(state.HOME)}
        gate = pathlib.Path(tempfile.mkdtemp(), "go")
        code = ("import sys,time,pathlib,json\nsys.path.insert(0, sys.argv[2])\n"
                "from tagteam import config, limits\ncfg = config.load()\ncfg['limits']['max_reviews_per_hour'] = 5\n"
                "while not pathlib.Path(sys.argv[3]).exists(): time.sleep(0.005)\n"
                "print(limits.reserve(sys.argv[1], 's', time.time(), cfg))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, f"p{i}", str(ROOT), str(gate)], env=env,
                                  stdout=subprocess.PIPE, text=True) for i in range(8)]
        time.sleep(0.5)
        gate.write_text("")
        outs = [p.communicate(timeout=20)[0].strip() for p in procs]
        self.assertEqual(sorted(outs), ["False"] * 7 + ["True"])
        self.assertEqual(len(RES.read_text().splitlines()), 5)

    def test_reservations_expire_after_an_hour(self):
        seed(5, age=3601)
        self.assertTrue(limits.reserve("new", "s", time.time(), cfg_max(5)))
        self.assertEqual(len(RES.read_text().splitlines()), 1)

    def test_crashed_run_still_counts(self):
        seed(5)                                       # reservations with no run record, as after a killed hook
        self.assertFalse(limits.reserve("new", "s", time.time(), cfg_max(5)))

    def test_state_error_means_no_reservation(self):
        with mock.patch.object(state, "locked", side_effect=OSError("disk")):
            self.assertFalse(limits.reserve("new", "s", time.time(), cfg_max(5)))

    def test_digest_ignores_order_and_changes_with_diff(self):
        a, b = {"path": "a", "diff": "1"}, {"path": "b", "diff": "2"}
        self.assertEqual(limits.digest([a, b]), limits.digest([b, a]))
        self.assertNotEqual(limits.digest([a, b]), limits.digest([a, {**b, "diff": "3"}]))


class Breaker(unittest.TestCase):
    def setUp(self):
        state.path("cooldowns.json").unlink(missing_ok=True)
        state.path("failures.json").unlink(missing_ok=True)

    def test_breaker_after_two_failures(self):
        limits.note_result("agy", False, CFG)
        self.assertFalse(quota.cooling("agy"))
        limits.note_result("agy", False, CFG)
        self.assertTrue(quota.cooling("agy"))

    def test_success_between_resets_the_count(self):
        limits.note_result("agy", False, CFG)
        limits.note_result("agy", True, CFG)
        limits.note_result("agy", False, CFG)
        self.assertFalse(quota.cooling("agy"))

    def test_runner_reports_failures(self):
        class R:
            def argv(self, *a):
                return ["x"], None, None

            def parse(self, *a):
                return None

            def usage(self, *a):
                return {}
        h = mock.Mock(reviewer=R())
        with mock.patch.object(runner.harnesses, "get", return_value=h), \
                mock.patch.object(runner, "_spawn", return_value=("", "boom", 1, False)):
            for _ in range(2):
                runner.run_one("agy", CFG, "p", ".", 100, tempfile.mkdtemp())
                if _ == 0:
                    self.assertFalse(quota.cooling("agy"))
        self.assertTrue(quota.cooling("agy"))


class Guards(HookRig, unittest.TestCase):
    def setUp(self):
        from tagteam import core
        self.rig()
        RES.unlink(missing_ok=True)
        state.path("runs.jsonl").unlink(missing_ok=True)
        write_user_config(use_toml(["copilot"]))
        self.core = core
        self.n = 0

    def tearDown(self):
        self.unrig()

    def start(self):
        self.run_hook("claude", "x", self.claude("UserPromptSubmit", prompt="add feature"))
        self.baseline = state.read_json(self.core._session_file(mock.Mock(agent="claude", session_id="sess1")))["baseline"]

    def stop(self, results=None, change=True):
        if change:
            self.n += 1
            pathlib.Path(self.repo, "feature.py").write_text(f"a = {self.n}\n" * 20)
        with mock.patch.object(runner, "run_all", self.fake_run_all(results or {})):
            return self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))

    def rewind(self):
        """Author reverts and redoes the same change: the next Stop sees the identical diff."""
        sf = self.core._session_file(mock.Mock(agent="claude", session_id="sess1"))
        s = state.read_json(sf)
        s["baseline"] = self.baseline
        state.write_json(sf, s)

    def blocker(self):
        return {"copilot": {"verdict": "request_changes", "summary": "", "findings": [finding("blocker")]}}

    def test_state_error_is_not_reviewed_with_zero_spawns(self):
        self.start()
        real = state.locked

        def locked(name="global"):
            if name == "limits":
                raise OSError("disk")
            return real(name)
        with mock.patch.object(state, "locked", locked):
            out = self.stop()
        self.assertEqual(self.calls, [])
        self.assertEqual(out["systemMessage"], NOTICE.format(n=30))

    def test_ceiling_reached_notice_and_status(self):
        write_user_config(use_toml(["copilot"]) + "[limits]\nmax_reviews_per_hour = 3\n")
        seed(3)
        self.start()
        out = self.stop()
        self.assertEqual(self.calls, [])
        self.assertEqual(out["systemMessage"], NOTICE.format(n=3))
        last = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual(last["reason"], "review limit reached (3 per hour)")

    def test_review_reserves_one_slot(self):
        self.start()
        self.stop()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(RES.read_text().splitlines()), 1)

    def test_route_reason_spends_no_reservation(self):
        write_user_config(use_toml([]))
        self.start()
        self.stop()
        self.assertFalse(RES.exists() and RES.read_text())

    def test_approved_digest_skips_review(self):
        self.start()
        self.stop()
        self.rewind()
        runs = len(state.read_jsonl(state.path("runs.jsonl")))
        self.assertEqual(self.stop(change=False), {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(state.read_jsonl(state.path("runs.jsonl"))), runs)
        self.assertEqual(len(RES.read_text().splitlines()), 1)

    def test_blocked_digest_reissues_block_and_counts_round(self):
        self.start()
        first = self.stop(self.blocker())
        self.assertEqual(first["decision"], "block")
        status = state.path("status.json").read_bytes()
        runs = state.read_jsonl(state.path("runs.jsonl"))
        self.rewind()
        second = self.stop(self.blocker(), change=False)
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(RES.read_text().splitlines()), 1)
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl")), runs)
        self.assertEqual(state.path("status.json").read_bytes(), status)
        sf = self.core._session_file(mock.Mock(agent="claude", session_id="sess1"))
        self.assertEqual(state.read_json(sf)["round"], 2)
        self.rewind()
        self.assertEqual(self.stop(self.blocker(), change=False), {})      # round 3 > max_rounds_per_turn
        self.assertEqual(len(self.calls), 1)

    def test_failed_run_records_no_digest(self):
        self.start()

        def failing(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            self.calls.append(jobs)
            return {r: ({"reviewer": r, "ok": False, "duration": 1}, None) for r in jobs}
        self.n += 1
        pathlib.Path(self.repo, "feature.py").write_text("a = 1\n" * 20)
        with mock.patch.object(runner, "run_all", failing):
            self.run_hook("claude", "x", self.claude("Stop", stop_hook_active=False))
        self.rewind()
        self.stop(change=False)
        self.assertEqual(len(self.calls), 2)

    def test_digest_memory_is_capped_at_50(self):
        self.start()
        for i in range(60):
            limits.remember("claude-sess1", f"d{i}", "allow", [], "m")
        sf = self.core._session_file(mock.Mock(agent="claude", session_id="sess1"))
        d = state.read_json(sf)["digests"]
        self.assertEqual((len(d), "d0" in d, "d59" in d), (50, False, True))


class Concurrency(unittest.TestCase):
    SCRIPT = '''
import io, json, os, pathlib, sys, time
from unittest import mock
sys.path.insert(0, sys.argv[1])
from tagteam import cli, core, harnesses, runner
mode, sid, repo, pids, gate = sys.argv[2:7]
base = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": repo, "prompt_id": "p",
        "transcript_path": "/x/y.jsonl"}
def fake(jobs, cfg, cwd, run_dir, deadline, on_done=None):
    with open(pids, "a") as f:
        f.write(f"{os.getpid()}\\n")
    time.sleep(1)
    out = {r: ({"reviewer": r, "ok": True, "duration": 1}, {"verdict": "approve", "summary": "", "findings": []})
           for r in jobs}
    for r, (m, _) in out.items():
        on_done(r, m)
    return out
with mock.patch.object(harnesses, "detected", return_value=["claude", "copilot"]), \\
        mock.patch.object(core, "headroom_map", return_value={}), mock.patch.object(runner, "run_all", fake):
    if mode == "start":
        sys.stdin = io.StringIO(json.dumps({**base, "prompt": "add"}))
    else:
        while not pathlib.Path(gate).exists():
            time.sleep(0.005)
        sys.stdin = io.StringIO(json.dumps({**base, "hook_event_name": "Stop", "stop_hook_active": False}))
    cli.hook("claude", "x")
'''

    def test_concurrent_trigger_same_session_skipped(self):
        home = tempfile.mkdtemp()
        pathlib.Path(home, "config.toml").write_text(use_toml(["copilot"]))
        repo = make_repo()
        work = pathlib.Path(tempfile.mkdtemp())
        script, pids, gate = work / "s.py", work / "pids", work / "go"
        script.write_text(self.SCRIPT)
        env = {**os.environ, "TAGTEAM_HOME": home}
        env.pop("TAGTEAM_DEPTH", None)
        run = lambda mode, sid: subprocess.Popen([sys.executable, str(script), str(ROOT), mode, sid, repo,  # noqa: E731
                                                  str(pids), str(gate)], env=env, stdout=subprocess.PIPE, text=True)
        for sid in ("s", "t"):
            run("start", sid).communicate(timeout=20)
        pathlib.Path(repo, "feature.py").write_text("a = 1\n" * 20)
        procs = {name: run("stop", sid) for name, sid in (("s1", "s"), ("s2", "s"), ("t", "t"))}
        time.sleep(0.7)
        gate.write_text("")
        outs = {n: json.loads(p.communicate(timeout=20)[0].strip().splitlines()[-1]) for n, p in procs.items()}
        self.assertEqual(len(pids.read_text().split()), 2)                 # one review for s, one for t
        self.assertEqual(sum(outs[k] == {} for k in ("s1", "s2")), 1)
        self.assertNotEqual(outs["t"], {})


if __name__ == "__main__":
    unittest.main()
