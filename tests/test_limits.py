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

from tagteam import config, limits, live, quota, router, runner

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTICE = "[tagteam] ⚠ Not reviewed: review limit reached ({n} per hour)"


def wait_for(paths, timeout=20):
    end = time.time() + timeout
    while not all(pathlib.Path(p).exists() for p in paths):
        if time.time() > end:
            raise TimeoutError(f"never appeared: {paths}")
        time.sleep(0.005)


def res():
    return state.path("reservations.jsonl")


def cfg_max(n):
    return {**CFG, "limits": {**CFG["limits"], "max_reviews_per_hour": n}}


def seed(n, age=0.0):
    now = time.time()
    res().write_text("".join(json.dumps({"run_id": f"r{i}", "session": "x", "ts": now - age}) + "\n" for i in range(n)))


class Reservations(unittest.TestCase):
    def setUp(self):
        res().unlink(missing_ok=True)

    def test_default_ceiling_is_30(self):
        self.assertEqual(CFG["limits"]["max_reviews_per_hour"], 30)

    def test_parallel_reservations_never_exceed_ceiling(self):
        seed(4)
        env = {**os.environ, "TAGTEAM_HOME": str(state.HOME)}
        work = pathlib.Path(tempfile.mkdtemp())
        code = ("import sys,time,pathlib\nsys.path.insert(0, sys.argv[2])\n"
                "from tagteam import config, limits\ncfg = config.load()\ncfg['limits']['max_reviews_per_hour'] = 5\n"
                "w = pathlib.Path(sys.argv[3])\n(w / ('ready' + sys.argv[1])).write_text('')\n"
                "while not (w / 'go').exists(): time.sleep(0.002)\n"
                "print(limits.reserve(sys.argv[1], 's', time.time(), cfg) is None)\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(i), str(ROOT), str(work)], env=env,
                                  stdout=subprocess.PIPE, text=True) for i in range(8)]
        wait_for([work / f"ready{i}" for i in range(8)])
        (work / "go").write_text("")
        outs = [p.communicate(timeout=20)[0].strip() for p in procs]
        self.assertEqual(sorted(outs), ["False"] * 7 + ["True"])
        self.assertEqual(len(res().read_text().splitlines()), 5)

    def test_reservations_expire_after_an_hour(self):
        seed(5, age=3601)
        self.assertIsNone(limits.reserve("new", "s", time.time(), cfg_max(5)))
        self.assertEqual(len(res().read_text().splitlines()), 1)

    def test_crashed_run_still_counts(self):
        seed(5)                                       # reservations with no run record, as after a killed hook
        self.assertEqual(limits.reserve("new", "s", time.time(), cfg_max(5)), "review limit reached (5 per hour)")

    def test_state_error_means_no_reservation(self):
        with mock.patch.object(state, "locked", side_effect=OSError("disk")):
            self.assertEqual(limits.reserve("new", "s", time.time(), cfg_max(5)), limits.STATE_ERROR)

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
            for attempt in range(2):
                runner.run_one("agy", CFG, "p", ".", 100, tempfile.mkdtemp())
                if attempt == 0:
                    self.assertFalse(quota.cooling("agy"))
        self.assertTrue(quota.cooling("agy"))

    def test_breaker_state_error_does_not_break_the_run(self):
        h = mock.Mock(reviewer=mock.Mock(**{"argv.return_value": (["x"], None, None), "parse.return_value": None,
                                            "usage.return_value": {}}))
        real = state.locked

        def locked(name="global"):
            if name == "failures":
                raise OSError("disk")
            return real(name)
        with mock.patch.object(runner.harnesses, "get", return_value=h), \
                mock.patch.object(runner, "_spawn", return_value=("", "boom", 1, False)), \
                mock.patch.object(state, "locked", locked):
            rec, result = runner.run_one("agy", CFG, "p", ".", 100, tempfile.mkdtemp())
        self.assertEqual((rec["ok"], result), (False, None))


class Trips(unittest.TestCase):
    DAY = 86400

    def setUp(self):
        for n in ("disabled.json", "trips.json"):
            state.path(n).unlink(missing_ok=True)

    def disable_at(self, t, *a, **kw):
        with mock.patch("time.time", return_value=t):
            limits.disable(*a, **kw)

    def test_first_trip_is_canary_eligible(self):
        limits.disable("copilot", "x", "r1")
        self.assertEqual(limits.canary_eligible(), ["copilot"])
        self.assertIs(limits.trips()["copilot"]["latched"], False)
        self.assertEqual(limits.trips()["copilot"]["trips"][0]["run_id"], "r1")

    def test_second_trip_within_7d_latches(self):
        now = time.time()
        self.disable_at(now, "copilot", "x", "r1")
        self.disable_at(now + 3 * self.DAY, "copilot", "x", "r2")
        self.assertIs(limits.trips()["copilot"]["latched"], True)
        self.assertEqual(limits.canary_eligible(), [])

    def test_trips_8d_apart_do_not_latch(self):
        now = time.time()
        self.disable_at(now, "copilot", "x", "r1")
        self.disable_at(now + 8 * self.DAY, "copilot", "x", "r2")
        self.assertIs(limits.trips()["copilot"]["latched"], False)
        self.assertEqual(limits.trip_count("copilot"), 2)
        self.assertEqual(limits.canary_eligible(), ["copilot"])

    def test_old_disabled_entry_without_trips_is_eligible(self):
        state.path("disabled.json").write_text(json.dumps({"copilot": {"reason": "x", "ts": time.time() - 15 * 86400}}))
        self.assertEqual(limits.canary_eligible(), ["copilot"])
        self.assertEqual(limits.trip_count("copilot"), 1)

    def test_canary_write_latches_old_and_migrated(self):
        now = time.time()
        self.disable_at(now - 15 * self.DAY, "copilot", "x", "r1")
        limits.disable("copilot", "canary", "c1", latch=True)
        self.assertIs(limits.trips()["copilot"]["latched"], True)
        self.assertEqual(limits.canary_eligible(), [])
        state.path("disabled.json").write_text(json.dumps({"codex": {"reason": "x", "ts": now - 15 * self.DAY}}))
        limits.disable("codex", "canary", "c2", latch=True)
        self.assertIs(limits.trips()["codex"]["latched"], True)
        self.assertEqual(limits.canary_eligible(), [])

    def test_reenable_refuses_after_newer_trip_or_latch(self):
        now = time.time()
        self.disable_at(now - 20 * self.DAY, "copilot", "x", "r1")
        n = limits.trip_count("copilot")
        self.disable_at(now, "copilot", "x", "r2")
        self.assertFalse(limits.reenable("copilot", n))
        self.assertIn("copilot", limits.disabled())
        self.assertIs(limits.trips()["copilot"]["latched"], False)
        limits.disable("copilot", "canary", "c1", latch=True)
        self.assertFalse(limits.reenable("copilot", limits.trip_count("copilot") - 1))
        self.assertFalse(limits.reenable("copilot", limits.trip_count("copilot")))      # latched
        self.assertIn("copilot", limits.disabled())

    def test_reenable_keeps_history(self):
        now = time.time()
        self.disable_at(now, "copilot", "x", "r1")
        self.assertTrue(limits.reenable("copilot", limits.trip_count("copilot")))
        self.assertNotIn("copilot", limits.disabled())
        self.assertEqual(len(limits.trips()["copilot"]["trips"]), 1)
        self.disable_at(now + 2 * self.DAY, "copilot", "x", "r2")
        self.assertIs(limits.trips()["copilot"]["latched"], True)

    def test_unreadable_trips_blocks_canary_and_is_not_rewritten(self):
        state.path("trips.json").write_text("{")
        limits.disable("copilot", "x", "r1")
        self.assertIn("copilot", limits.disabled())
        self.assertEqual(state.path("trips.json").read_text(), "{")
        self.assertIsNone(limits.trips())
        self.assertEqual(limits.canary_eligible(), [])
        self.assertIsNone(limits.trip_count("copilot"))
        self.assertFalse(limits.reenable("copilot", 1))
        self.assertIn("copilot", limits.disabled())

    def lost_trip_then_reenable(self, age, break_trips):
        now = time.time()
        self.disable_at(now - age * self.DAY, "copilot", "x", "r1")
        n = limits.trip_count("copilot")
        with break_trips():
            limits.disable("copilot", "x", "r2")
        self.assertEqual(limits.trips()["copilot"]["trips"][-1]["run_id"], "r1")      # the new trip really was lost
        self.assertFalse(limits.reenable("copilot", n))
        self.assertIn("copilot", limits.disabled())

    def trips_none_once(self):
        real, calls = limits.trips, []
        return mock.patch.object(limits, "trips", lambda: None if not calls.append(1) and len(calls) == 1 else real())

    def trips_write_fails_once(self):
        real = state.write_json

        def write(p, data):
            if pathlib.Path(p).name == "trips.json":
                raise OSError("disk")
            real(p, data)
        return mock.patch.object(state, "write_json", write)

    def test_lost_trip_read_failure_blocks_reenable(self):
        self.lost_trip_then_reenable(10, self.trips_none_once)

    def test_lost_trip_write_failure_blocks_reenable(self):
        self.lost_trip_then_reenable(10, self.trips_write_fails_once)

    def test_lost_trip_within_7d_of_previous_latches(self):
        self.lost_trip_then_reenable(3, self.trips_write_fails_once)
        self.assertEqual(limits.canary_eligible(), [])
        self.assertEqual(limits.trip_count("copilot"), 2)

    def test_lost_trip_8d_after_previous_stays_eligible(self):
        self.lost_trip_then_reenable(8, self.trips_write_fails_once)
        self.assertEqual(limits.canary_eligible(), ["copilot"])
        self.assertEqual(limits.trip_count("copilot"), 2)

    def test_disable_records_the_repo(self):
        limits.disable("copilot", "x", "r1", repo="/r")
        self.assertEqual(limits.disabled()["copilot"]["repo"], "/r")
        self.assertEqual(limits.trips()["copilot"]["trips"][-1], {"ts": limits.disabled()["copilot"]["ts"],
                                                                  "run_id": "r1", "repo": "/r"})

    def test_canary_latch_survives_a_failed_trips_write(self):
        self.disable_at(time.time() - 10 * self.DAY, "copilot", "x", "r1")
        with self.trips_write_fails_once():
            limits.disable("copilot", "wrote in canary", "canary", latch=True)
        self.assertEqual(limits.trips()["copilot"]["trips"][-1]["run_id"], "r1")      # the canary trip was lost
        self.assertEqual(limits.canary_eligible(), [])
        self.assertIs(limits.disabled()["copilot"].get("latched"), True)
        self.assertFalse(limits.reenable("copilot", limits.trip_count("copilot")))

    def test_recorded_trip_is_not_double_counted(self):
        limits.disable("copilot", "x", "r1")
        self.assertEqual(limits.trip_count("copilot"), 1)
        self.assertTrue(limits.reenable("copilot", 1))

    def test_unreadable_disabled_blocks_canary(self):
        limits.disable("copilot", "x", "r1")
        state.path("disabled.json").write_text("{")
        self.assertEqual(limits.canary_eligible(), [])

    def test_non_dict_trip_item_fails_closed(self):
        limits.disable("copilot", "x", "r1")
        t = limits.trips()
        t["copilot"]["trips"].append("garbage")
        state.write_json(state.path("trips.json"), t)
        self.assertEqual((limits.canary_eligible(), limits.trip_count("copilot")), ([], None))
        self.assertFalse(limits.reenable("copilot", 2))
        limits.disable("copilot", "again", "r2")                                    # no crash; history left as is
        self.assertEqual(limits.disabled()["copilot"]["reason"], "again")
        self.assertEqual(limits.trips(), t)

    def test_set_repo_fills_a_missing_repo_only(self):
        state.write_json(state.path("disabled.json"), {"copilot": {"reason": "x", "ts": 5, "latched": True},
                                                       "codex": {"reason": "y", "ts": 6, "repo": "/old"}})
        limits.set_repo("copilot", "/r")
        limits.set_repo("codex", "/r")
        limits.set_repo("agy", "/r")
        self.assertEqual(limits.disabled(), {"copilot": {"reason": "x", "ts": 5, "latched": True, "repo": "/r"},
                                             "codex": {"reason": "y", "ts": 6, "repo": "/old"}})
        state.path("disabled.json").write_text("{")
        limits.set_repo("copilot", "/r")
        self.assertEqual(state.path("disabled.json").read_text(), "{")

    def test_clear_resets_trips(self):
        limits.disable("copilot", "x", "r1")
        limits.clear_disabled()
        self.assertEqual(limits.trips(), {})


class Guards(HookRig, unittest.TestCase):
    def setUp(self):
        from tagteam import core
        self.rig()
        res().unlink(missing_ok=True)
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
        self.assertEqual(out["systemMessage"], "[tagteam] ⚠ Not reviewed: state error, see tagteam.log")

    def test_unreadable_safety_list_fails_closed(self):
        for name in ("disabled.json", "cooldowns.json"):
            with self.subTest(name):
                self.calls.clear()
                state.path(name).write_text("{not json")
                self.start()
                out = self.stop()
                self.assertEqual(self.calls, [])
                self.assertEqual(out["systemMessage"], "[tagteam] ⚠ Not reviewed: state error, see tagteam.log")
                limits.disable("codex", "x")
                quota.cooldown("codex", 15)
                self.assertEqual(state.path(name).read_text(), "{not json")     # never "repaired" to one entry
                if name == "disabled.json":
                    self.assertEqual(router.candidates("claude", config.load()), [])
                else:
                    self.assertTrue(quota.cooling("copilot"))
                state.path(name).unlink()
                state.path("disabled.json").unlink(missing_ok=True)
                state.path("cooldowns.json").unlink(missing_ok=True)

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
        self.assertEqual(len(res().read_text().splitlines()), 1)

    def test_route_reason_spends_no_reservation(self):
        write_user_config(use_toml([]))
        self.start()
        self.stop()
        self.assertFalse(res().exists() and res().read_text())

    def test_approved_digest_skips_review(self):
        self.start()
        self.stop()
        self.rewind()
        runs = len(state.read_jsonl(state.path("runs.jsonl")))
        self.assertEqual(self.stop(change=False), {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(state.read_jsonl(state.path("runs.jsonl"))), runs)
        self.assertEqual(len(res().read_text().splitlines()), 1)

    def test_blocked_digest_reissues_block_and_counts_round(self):
        self.start()
        first = self.stop(self.blocker())
        self.assertEqual(first["decision"], "block")
        today = state.read_json(state.path("status.json"))["today"]
        runs = state.read_jsonl(state.path("runs.jsonl"))
        self.rewind()
        second = self.stop(self.blocker(), change=False)
        self.assertEqual(second, first)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(res().read_text().splitlines()), 1)
        recorded = state.read_jsonl(state.path("runs.jsonl"))          # the re-issue is shown, but spawned and spent nothing
        self.assertEqual(recorded[:-1], runs)
        self.assertEqual((recorded[-1]["round"], recorded[-1]["reviewers"], recorded[-1]["blocking"]), (2, [], 1))
        self.assertEqual(state.read_json(state.path("status.json"))["today"], today)
        self.assertIn("1 issue still open after fixes (round 2/2)", live.segment("claude", "sess1"))
        sf = self.core._session_file(mock.Mock(agent="claude", session_id="sess1"))
        self.assertEqual(state.read_json(sf)["round"], 2)
        self.rewind()
        third = self.stop(self.blocker(), change=False)                    # round 3 is the confirm round: no spawn, no block
        self.assertEqual(third, {"systemMessage": self.core.BACKGROUND})
        self.assertIn("⚠ Unresolved: 1 issue still open after 2 fix rounds", live.segment("claude", "sess1"))
        self.assertEqual(len(self.calls), 1)
        last = state.read_jsonl(state.path("runs.jsonl"))[-1]
        self.assertEqual((last["confirm"], last["reviewers"], last["blocking"]), (True, [], 1))
        self.assertEqual(self.stop(self.blocker(), change=False), {})      # round 4: the turn is over

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

    def test_session_lock_error_is_not_reviewed_notice(self):
        self.start()
        import fcntl
        real_flock = fcntl.flock

        def flaky(f, op):
            if op & fcntl.LOCK_NB:
                raise OSError(5, "io")
            return real_flock(f, op)
        with mock.patch("fcntl.flock", flaky):
            out = self.stop()
        self.assertEqual(self.calls, [])
        self.assertEqual(out["systemMessage"], "[tagteam] ⚠ Not reviewed: state error, see tagteam.log")
        self.assertEqual(state.read_jsonl(state.path("runs.jsonl"))[-1]["reason"], "state error, see tagteam.log")
        self.calls.clear()
        real_open = open

        def bad_open(f, *a, **k):
            if "session-" in str(f):
                raise OSError("denied")
            return real_open(f, *a, **k)
        with mock.patch("builtins.open", bad_open):
            out = self.stop()
        self.assertEqual(self.calls, [])
        self.assertIn("state error", out["systemMessage"])

    def test_session_lock_contention_is_silent_not_error(self):
        with limits.session_lock("claude", "s") as (outer, _):
            with limits.session_lock("claude", "s") as (inner, _):
                self.assertEqual((outer, inner), (limits.HELD, limits.BUSY))

    def test_remember_state_error_keeps_the_block(self):
        self.start()
        key = self.core._session_file(mock.Mock(agent="claude", session_id="sess1")).stem
        real, raised = state.locked, []

        def locked(name="global"):
            if name == key and not raised:
                raised.append(1)
                raise OSError("disk")
            return real(name)
        runs = len(state.read_jsonl(state.path("runs.jsonl")))
        with mock.patch.object(state, "locked", locked):
            out = self.stop(self.blocker())
        self.assertEqual(raised, [1])
        self.assertEqual(out["decision"], "block")
        recorded = state.read_jsonl(state.path("runs.jsonl"))
        self.assertEqual((len(recorded) - runs, recorded[-1]["action"]), (1, "block"))

    def test_session_lock_held_before_turn_end_reads_state(self):
        """A rival trigger must see BUSY from the moment turn_end starts collecting, not only inside review."""
        self.start()
        seen = []
        real = self.core.collect

        def collect(s, cfg):
            with limits.session_lock("claude", "sess1") as (st, _):
                seen.append(st)
            return real(s, cfg)
        with mock.patch.object(self.core, "collect", collect):
            self.stop()
        self.assertEqual(seen, [limits.BUSY])

    def test_same_plan_twice_is_reviewed_once(self):
        self.start()
        plan = {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode", "session_id": "sess1",
                "cwd": self.repo, "tool_input": {"plan": "1. do a thing\n2. do another\n"}}
        for _ in range(2):
            with mock.patch.object(runner, "run_all", self.fake_run_all({})):
                self.run_hook("claude", "x", plan)
        self.assertEqual(len(self.calls), 1)

    def test_digest_memory_is_capped_at_50(self):
        self.start()
        sf = self.core._session_file(mock.Mock(agent="claude", session_id="sess1"))
        turn = state.read_json(sf)["turn_id"]
        for i in range(60):
            limits.remember("claude-sess1", f"d{i}", "allow", [], "m", turn)
        limits.remember("claude-sess1", "other-turn", "allow", [], "m", "another turn id")
        d = state.read_json(sf)["digests"]
        self.assertNotIn("other-turn", d)
        self.assertEqual((len(d), "d0" in d, "d59" in d), (50, False, True))


class Concurrency(unittest.TestCase):
    SCRIPT = '''
import io, json, os, pathlib, sys, time
from unittest import mock
sys.path.insert(0, sys.argv[1])
from tagteam import cli, core, harnesses, runner
mode, name, sid, repo, work = sys.argv[2:7]
work = pathlib.Path(work)
base = {"hook_event_name": "UserPromptSubmit", "session_id": sid, "cwd": repo, "prompt_id": "p",
        "transcript_path": "/x/y.jsonl"}
def wait(path):
    end = time.time() + 30
    while not path.exists() and time.time() < end:
        time.sleep(0.002)
def fake(jobs, cfg, cwd, run_dir, deadline, on_done=None):
    with open(work / "pids", "a") as f:
        f.write(f"{os.getpid()}\\n")
    wait(work / "release")
    out = {r: ({"reviewer": r, "ok": True, "duration": 1}, {"verdict": "approve", "summary": "", "findings": []})
           for r in jobs}
    for r, (m, _) in out.items():
        on_done(r, m)
    return out
with mock.patch.object(harnesses, "detected", return_value=["claude", "copilot"]), \\
        mock.patch.object(core, "headroom_map", return_value={}), mock.patch.object(runner, "run_all", fake):
    if mode == "start":
        payload = {**base, "prompt": "add"}
    else:
        (work / ("ready-" + name)).write_text("")
        wait(work / "go")
        payload = {**base, "hook_event_name": "Stop", "stop_hook_active": False}
    sys.stdin = io.StringIO(json.dumps(payload))
    cli.hook("claude", "x")
'''

    def test_concurrent_trigger_same_session_waits_then_finds_nothing_new(self):
        home = tempfile.mkdtemp()
        pathlib.Path(home, "config.toml").write_text(use_toml(["copilot"]) + "[review]\nblocking = { small = true }\n"
                                                     "[ledger]\nattribute = false\n")
        repo = make_repo()
        work = pathlib.Path(tempfile.mkdtemp())
        script = work / "s.py"
        script.write_text(self.SCRIPT)
        env = {**os.environ, "TAGTEAM_HOME": home}
        env.pop("TAGTEAM_DEPTH", None)
        run = lambda mode, name, sid: subprocess.Popen([sys.executable, str(script), str(ROOT), mode, name, sid,  # noqa: E731
                                                        repo, str(work)], env=env, stdout=subprocess.PIPE, text=True)
        for sid in ("s", "t"):
            run("start", "start", sid).communicate(timeout=20)
        pathlib.Path(repo, "feature.py").write_text("a = 1\n" * 20)
        procs = {name: run("stop", name, sid) for name, sid in (("s1", "s"), ("s2", "s"), ("t", "t"))}
        wait_for([work / f"ready-{n}" for n in procs])
        (work / "go").write_text("")
        wait_for([work / "pids"])
        time.sleep(1)
        self.assertEqual([procs[k].poll() for k in ("s1", "s2")], [None, None])   # the loser waits for the winner
        (work / "release").write_text("")
        outs = {n: json.loads(p.communicate(timeout=20)[0].strip().splitlines()[-1]) for n, p in procs.items()}
        self.assertEqual(len((work / "pids").read_text().split()), 2)         # one review for s, one for t
        self.assertEqual(sum(outs[k] == {} for k in ("s1", "s2")), 1)
        self.assertNotEqual(outs["t"], {})


if __name__ == "__main__":
    unittest.main()
