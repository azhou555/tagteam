import contextlib
import dataclasses
import io
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.helpers import ALL_AGENTS, CFG, HookRig, make_repo, write_user_config  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import doctor, harnesses, heal, health, limits, quota, sandbox, state  # noqa: E402
from tagteam.harnesses.base import TAG  # noqa: E402

DAY = 86400
REAL_PROBE = doctor.probe


def cfg(use=("codex", "agy", "copilot"), authors=()):
    return {**CFG, "reviewers": {**CFG["reviewers"], "use": list(use)}, "authors": {"review": list(authors)}}


def demote(name):
    for i in range(4):
        state.append_jsonl(state.path("history.jsonl"), {"ts": time.time() - 100 + i, "reviewer": name, "ok": False})


def marker_dir(files):
    cmd = json.loads(files[".github/hooks/tagteam-canary.json"])["hooks"]["agentStop"][0]["bash"]
    return os.path.dirname(shlex.split(cmd)[1])


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def rev(name):
    return health.load()["reviewers"][name]


def acts(events):
    return [f"{e['action']} {e['reviewer']}" for e in events]


class Rig:
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.repo = make_repo()
        self.found, self.logged, self.result, self.probes = list(ALL_AGENTS), {}, {}, []
        self.ps = [mock.patch.object(harnesses, "detected", side_effect=lambda: list(self.found)),
                   mock.patch.object(harnesses, "ALL", {n: dataclasses.replace(
                       h, hooks_path=lambda n=n: self.d / f"{n}.json", logged_in=lambda n=n: self.logged.get(n, True),
                       hook_trust=None) for n, h in harnesses.ALL.items()}),
                   mock.patch.object(doctor, "probe", self.fake_probe)]
        for p in self.ps:
            p.start()
            self.addCleanup(p.stop)

    def fake_probe(self, name, cfg, repo=None, files=None, stale_s=7200):
        self.probes.append((name, files is not None))
        self.probed_repo = repo
        r = self.result.get(name, ("ok", ""))
        return r(name, files) if callable(r) else r

    def canary(self, status="ok", touch=None, during=None):
        def run(name, files):
            self.files = files
            if touch:
                pathlib.Path(marker_dir(files), touch).touch()
            if during:
                during()
            return status, ""
        return run


class Tick(Rig, unittest.TestCase):
    def test_tick_on_empty_state(self):
        self.assertEqual(heal.tick(cfg()), [])
        self.assertIsInstance(health.load()["last_tick"], float)
        self.assertEqual(self.probes, [])

    def test_demoted_probe_pass_restores(self):
        demote("agy")
        events = heal.tick(cfg())
        self.assertEqual(acts(events), ["demoted agy", "restored agy"])
        self.assertEqual(self.probes, [("agy", False)])
        self.assertFalse(health.is_demoted("agy"))
        self.assertEqual(rev("agy")["status"], "ok")
        self.assertEqual(state.read_jsonl(state.path("probes.jsonl"))[-1]["kind"], "probe")

    def test_probe_auth_error_needs_you(self):
        demote("agy")
        self.result["agy"] = ("failed", "Error: 401 Unauthorized")
        events = heal.tick(cfg())
        self.assertEqual(rev("agy")["status"], "needs_you")
        self.assertIn("log in", rev("agy")["reason"])
        self.assertIn("needs_you agy", acts(events))
        self.result["agy"] = ("timeout", "")
        heal.tick(cfg(), now=time.time() + 3600)
        self.assertEqual(rev("agy")["status"], "demoted")       # a later non-auth failure drops the stale auth reason

    def test_probe_failure_backs_off(self):
        demote("agy")
        self.result["agy"] = ("timeout", "")
        t0 = time.time()
        heal.tick(cfg(), now=t0)
        self.assertEqual((rev("agy")["probe_backoff_min"], rev("agy")["next_probe"]), (15, t0 + 15 * 60))
        heal.tick(cfg(), now=t0 + 16 * 60)
        self.assertEqual(rev("agy")["probe_backoff_min"], 30)
        self.assertEqual(len(self.probes), 2)
        heal.tick(cfg(), now=t0 + 17 * 60)
        self.assertEqual(len(self.probes), 2)
        self.assertEqual(rev("agy")["status"], "demoted")

    def test_paused_and_disabled_reviewers_get_no_plain_probe(self):
        demote("agy")
        quota.cooldown("agy", 15)
        demote("codex")
        limits.disable("codex", "x", "r1", repo=self.repo)
        self.result["codex"] = self.canary(status="timeout")
        heal.tick(cfg())
        self.assertEqual(self.probes, [("codex", True)])
        self.assertEqual((rev("agy")["status"], rev("codex")["status"]), ("paused", "disabled"))

    def test_backoff_caps_at_six_hours(self):
        demote("agy")
        self.result["agy"] = ("failed", "boom")
        t = time.time()
        for _ in range(8):
            heal.tick(cfg(), now=t)
            t = rev("agy")["next_probe"]
        self.assertEqual(rev("agy")["probe_backoff_min"], 360)

    def test_max_two_probes_and_copilot_daily_cap(self):
        for n in ("codex", "agy", "copilot"):
            demote(n)
        self.result = {n: ("failed", "x") for n in ("codex", "agy", "copilot")}
        heal.tick(cfg())
        self.assertEqual([p[0] for p in self.probes], ["codex", "agy"])
        self.probes.clear()
        for _ in range(4):
            health.note_probe("copilot", False, "probe")
        heal.tick(cfg(use=["copilot"]))
        self.assertEqual(self.probes, [])

    def test_canary_pass_reenables(self):
        limits.disable("copilot", "wrote outside read-only", "r1", repo=self.repo)
        self.result["copilot"] = self.canary()
        events = heal.tick(cfg())
        self.assertEqual(self.probes, [("copilot", True)])
        self.assertNotIn("copilot", limits.disabled())
        self.assertEqual(limits.trips()["copilot"]["trips"][0]["run_id"], "r1")
        self.assertIn("re-enabled copilot", acts(events))
        self.assertEqual(rev("copilot")["status"], "ok")
        self.assertEqual(self.probed_repo, self.repo)
        self.assertEqual(sorted(self.files), [".claude/settings.json", ".github/hooks/tagteam-canary.json",
                                              ".github/mcp.json", ".mcp.json"])

    def test_canary_marker_means_trip_and_latch(self):
        with mock.patch("time.time", return_value=time.time() - 10 * DAY):      # an old trip: only the canary latches
            limits.disable("copilot", "x", "r1", repo=self.repo)
        self.result["copilot"] = self.canary(touch="mcp")
        events = heal.tick(cfg())
        self.assertIn("copilot", limits.disabled())
        self.assertIs(limits.trips()["copilot"]["latched"], True)
        self.assertEqual(limits.trips()["copilot"]["trips"][-1]["run_id"], "canary")
        self.assertEqual(rev("copilot")["status"], "needs_you")
        self.assertIn("needs_you copilot", acts(events))
        heal.tick(cfg(), now=time.time() + DAY)
        self.assertEqual(len(self.probes), 1)          # latched: no more canaries

    def test_canary_wrote_files_means_trip(self):
        limits.disable("copilot", "x", "r1", repo=self.repo)
        self.result["copilot"] = self.canary(status="wrote-files")
        heal.tick(cfg())
        self.assertIs(limits.trips()["copilot"]["latched"], True)

    def test_canary_inconclusive_stays_disabled(self):
        limits.disable("copilot", "x", "r1", repo=self.repo)
        self.result["copilot"] = self.canary(status="timeout")
        t0 = time.time()
        heal.tick(cfg(), now=t0)
        self.assertIn("copilot", limits.disabled())
        self.assertIs(limits.trips()["copilot"]["latched"], False)
        self.assertEqual(rev("copilot")["next_probe"], t0 + 15 * 60)
        self.assertEqual(rev("copilot")["status"], "disabled")
        heal.tick(cfg(), now=t0 + 60)
        self.assertEqual(len(self.probes), 1)

    def test_unlisted_reviewer_gets_no_canary_or_probe(self):
        limits.disable("claude", "x", "r1", repo=self.repo)
        demote("opencode")
        heal.tick(cfg())
        self.assertEqual(self.probes, [])

    def test_prune_old_runs_not_locks(self):
        runs = state.path("runs", "x").parent
        now = time.time()
        for name, age in (("old", 31), ("new", 29)):
            (runs / name).mkdir(parents=True)
            (runs / name / "request.json").write_text("{}")
            os.utime(runs / name, (now - age * DAY, now - age * DAY))
        lock = state.path("locks", "session-x.lock")
        lock.write_text("")
        os.utime(lock, (now - 90 * DAY, now - 90 * DAY))
        events = heal.tick(cfg(), now=now)
        self.assertEqual(sorted(p.name for p in runs.iterdir()), ["new"])
        self.assertTrue(lock.exists())
        self.assertIn("pruned None", acts(events))

    def test_missing_cli_and_not_logged_in_need_you(self):
        self.found.remove("copilot")
        self.logged["agy"] = False
        events = heal.tick(cfg())
        self.assertEqual((rev("copilot")["status"], rev("agy")["status"]), ("needs_you", "needs_you"))
        self.assertEqual(sorted(acts(events)), ["needs_you agy", "needs_you copilot"])
        self.found.append("copilot")
        self.logged["agy"] = True
        events = heal.tick(cfg())
        self.assertEqual((rev("copilot")["status"], rev("agy")["status"]), ("ok", "ok"))
        self.assertEqual(sorted(acts(events)), ["restored agy", "restored copilot"])

    def test_no_reviewer_for_author_needs_you(self):
        entry = self.d / "tagteam"
        entry.write_text("")
        (self.d / "claude.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"command": f": {TAG}; {entry} hook claude turn-end"}]}]}}))
        for n in ("codex", "agy"):
            limits.disable(n, "x", "r", repo=self.repo)
        self.result = {n: self.canary(status="timeout") for n in ("codex", "agy")}
        heal.tick(cfg(use=["codex", "agy"], authors=["claude"]))
        a = health.load()["authors"]["claude"]
        self.assertEqual(a["status"], "needs_you")
        self.assertIn("no listed reviewer", a["reason"])
        self.assertEqual(rev("codex")["status"], "disabled")      # author entries don't overwrite reviewer ones

    def test_stale_hooks_need_you(self):
        (self.d / "codex.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"command": f": {TAG}; {self.d / 'gone' / 'tagteam'} hook codex turn-end"}]}]}}))
        heal.tick(cfg())
        self.assertIn("tagteam install", health.load()["authors"]["codex"]["reason"])

    def test_concurrent_ticks_serialize(self):
        limits.disable("copilot", "x", "r1", repo=self.repo)
        inside, release = threading.Event(), threading.Event()
        self.result["copilot"] = self.canary(status="timeout", during=lambda: (inside.set(), release.wait(10)))
        t = threading.Thread(target=heal.tick, args=(cfg(),))
        t.start()
        self.assertTrue(inside.wait(10))
        self.assertIsNone(heal.tick(cfg()))
        release.set()
        t.join()
        self.assertEqual(heal.tick(cfg()), [])

    def test_canary_pass_does_not_undo_a_newer_trip(self):
        with mock.patch("time.time", return_value=time.time() - 10 * DAY):
            limits.disable("copilot", "x", "r1", repo=self.repo)
        self.result["copilot"] = self.canary(during=lambda: limits.disable("copilot", "x", "r2", repo=self.repo))
        heal.tick(cfg())
        self.assertIn("copilot", limits.disabled())
        self.assertIs(limits.trips()["copilot"]["latched"], False)

    def test_prune_worktrees_under_registry_lock(self):
        repo = make_repo()
        run = state.path("runs", "r1", "request.json")
        state.write_json(run, {"repo": repo})
        state.append_jsonl(state.path("runs.jsonl"), {"ts": time.time(), "run_id": "r1"})
        state.append_jsonl(state.path("runs.jsonl"), {"ts": time.time() - 8 * DAY, "run_id": "old"})
        held, calls, real_locked = [], [], state.locked

        @contextlib.contextmanager
        def locked(name="global"):
            with real_locked(name):
                held.append(name)
                try:
                    yield
                finally:
                    held.remove(name)
        c = {**cfg(), "review": {**CFG["review"], "deadline_s": 5000}}
        with mock.patch.object(state, "locked", locked), \
                mock.patch.object(sandbox, "_clear_stale", lambda r, s: calls.append((r, s, list(held)))):
            heal.tick(c)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], (repo, 10000))
        self.assertIn(sandbox._registry_lock(repo), calls[0][2])

    def test_prune_skips_a_broken_repo(self):
        state.write_json(state.path("runs", "r1", "request.json"), {"repo": "/nonexistent/repo"})
        state.append_jsonl(state.path("runs.jsonl"), {"ts": time.time(), "run_id": "r1"})
        self.assertEqual(heal.tick(cfg()), [])

    def test_migrated_entry_finds_its_repo_through_request_json(self):
        state.write_json(state.path("disabled.json"), {"copilot": {"reason": "wrote x (run 20261101T000000-ab12)",
                                                                   "ts": time.time() - 10 * DAY}})
        self.result["copilot"] = self.canary(status="timeout")
        heal.tick(cfg())
        self.assertEqual(rev("copilot")["status"], "needs_you")             # its run dir was pruned: no repo
        self.assertIn("can't re-test copilot: its repo is gone", rev("copilot")["reason"])
        self.assertEqual(self.probes, [])
        state.write_json(state.path("runs", "20261101T000000-ab12", "request.json"), {"repo": self.repo})
        heal.tick(cfg())
        self.assertEqual((self.probes, self.probed_repo), ([("copilot", True)], self.repo))
        self.assertEqual(rev("copilot")["status"], "disabled")


GOOD = json.dumps({"structured_output": {"verdict": "approve", "summary": "", "findings": []}})


class CanaryInRepo(Rig, unittest.TestCase):
    """The real doctor.probe on the trip repo; only the reviewer CLI (runner._spawn) is faked."""

    def setUp(self):
        super().setUp()
        self.spawned, self.run_hooks, self.probe_kw = [], None, []

        def probe(name, cfg, **kw):
            self.probe_kw.append(kw)
            return REAL_PROBE(name, cfg, **kw)

        def spawn(argv, stdin, cwd, timeout):
            self.spawned.append(cwd)
            if self.run_hooks:
                self.run_hooks(cwd)
            return GOOD, "", 0, False
        for p in (mock.patch.object(doctor, "probe", probe), mock.patch.object(doctor.runner, "_spawn", spawn)):
            p.start()
            self.addCleanup(p.stop)

    def git_state(self):
        return [git(self.repo, *a) for a in (["status", "--porcelain"], ["rev-parse", "HEAD"], ["ls-files", "-s"],
                                              ["for-each-ref"])]

    def test_canary_runs_in_a_worktree_of_the_trip_repo_and_leaves_it_alone(self):
        pathlib.Path(self.repo, "a.py").write_text("x = 2\n")             # the user's own work in progress
        pathlib.Path(self.repo, "staged.py").write_text("s\n")
        git(self.repo, "add", "staged.py")
        pathlib.Path(self.repo, "untracked.txt").write_text("u\n")
        before = self.git_state()
        seen = {}

        def look(cwd):
            seen.update(name=os.path.basename(cwd), a=pathlib.Path(cwd, "a.py").read_text(),
                        common=os.path.realpath(os.path.join(cwd, git(cwd, "rev-parse", "--git-common-dir").strip())),
                        traps=[f for f in (".claude/settings.json", ".github/hooks/tagteam-canary.json", ".mcp.json",
                                           ".github/mcp.json") if os.path.exists(os.path.join(cwd, f))])
        self.run_hooks = look
        limits.disable("claude", "x", "r1", repo=self.repo)
        events = heal.tick(cfg(use=["claude"]))
        self.assertTrue(seen["name"].startswith(sandbox.PREFIX))
        self.assertEqual(seen["common"], os.path.realpath(os.path.join(self.repo, ".git")))
        self.assertEqual(len(seen["traps"]), 4)
        self.assertEqual(seen["a"], "x = 1\n")                                 # HEAD, not the user's edit
        self.assertIn("re-enabled claude", acts(events))
        self.assertEqual(self.git_state(), before)
        self.assertFalse(os.path.exists(self.spawned[0]))

    def test_trip_repo_hook_writing_into_the_checkout_is_a_write(self):
        own = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo log >> course.log"}]}]}}
        pathlib.Path(self.repo, ".claude").mkdir()
        pathlib.Path(self.repo, ".claude/settings.json").write_text(json.dumps(own))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "course hooks")

        def run_repo_hooks(cwd):                      # a reviewer that runs the repo's own Stop hook, not our traps
            stop = json.loads(pathlib.Path(cwd, ".claude/settings.json").read_text())["hooks"]["Stop"]
            self.assertEqual(len(stop), 2)                                      # ours added, the repo's kept
            subprocess.run(stop[0]["hooks"][0]["command"], shell=True, cwd=cwd, check=True)
        self.run_hooks = run_repo_hooks
        with mock.patch("time.time", return_value=time.time() - 10 * DAY):
            limits.disable("claude", "x", "r1", repo=self.repo)
        heal.tick(cfg(use=["claude"]))
        self.assertIs(limits.trips()["claude"]["latched"], True)
        self.assertEqual(rev("claude")["status"], "needs_you")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "course.log")))

    def test_our_trap_running_is_a_write(self):
        self.run_hooks = lambda cwd: subprocess.run(json.loads(pathlib.Path(cwd, ".mcp.json").read_text())[
            "mcpServers"]["tagteam-canary"]["args"][1], shell=True, check=True)
        with mock.patch("time.time", return_value=time.time() - 10 * DAY):
            limits.disable("claude", "x", "r1", repo=self.repo)
        heal.tick(cfg(use=["claude"]))
        self.assertIs(limits.trips()["claude"]["latched"], True)

    def test_unknown_or_missing_repo_is_inconclusive_and_never_spawns(self):
        for repo in (None, "/nonexistent/tagteam-repo"):
            limits.clear_disabled()
            limits.disable("claude", "x", "r1", repo=repo)
            heal.tick(cfg(use=["claude"]))
            self.assertEqual(self.spawned, [])
            self.assertIn("claude", limits.disabled())
            self.assertEqual(rev("claude")["status"], "needs_you")
            self.assertEqual(rev("claude")["reason"],
                             "can't re-test claude: its repo is gone → tagteam doctor --clear")

    def test_no_sandbox_means_inconclusive_and_never_spawns(self):
        @contextlib.contextmanager
        def in_place(repo, tree, names, cwd=None, stale_s=7200):
            yield {n: cwd for n in names}
        limits.disable("claude", "x", "r1", repo=self.repo)
        t0 = time.time()
        with mock.patch.object(sandbox, "checkouts", in_place):
            heal.tick(cfg(use=["claude"]), now=t0)
        self.assertEqual(self.spawned, [])
        self.assertIn("claude", limits.disabled())
        self.assertIs(limits.trips()["claude"]["latched"], False)
        self.assertEqual(rev("claude")["next_probe"], t0 + 15 * 60)

    def test_stale_s_reaches_the_worktree_cleanup(self):
        calls, real = [], sandbox._clear_stale
        limits.disable("claude", "x", "r1", repo=self.repo)
        c = {**cfg(use=["claude"]), "review": {**CFG["review"], "deadline_s": 5000}}
        with mock.patch.object(sandbox, "_clear_stale", lambda r, s: (calls.append(s), real(r, s))):
            heal.tick(c)
        self.assertEqual(self.probe_kw[0]["stale_s"], 10000)
        self.assertEqual(calls[0], 10000)


class Start(unittest.TestCase):
    def setUp(self):
        self.launched = []
        p = mock.patch.object(heal, "_launch", lambda: self.launched.append(1))
        p.start()
        self.addCleanup(p.stop)

    def test_maybe_start_launches_once_under_contention(self):
        go = threading.Barrier(2)
        out = []

        def start():
            go.wait()
            out.append(heal.maybe_start())
        ts = [threading.Thread(target=start) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual((len(self.launched), sorted(out)), (1, [False, True]))

    def test_maybe_start_not_due(self):
        now = time.time()
        health.save({"last_tick": now - 60})
        self.assertFalse(heal.maybe_start(now))
        self.assertEqual(self.launched, [])
        self.assertTrue(heal.maybe_start(now + heal.TICK_S))
        self.assertEqual(health.load()["last_tick"], now + heal.TICK_S)

    def test_maybe_start_never_raises(self):
        state.path("health.json").write_text('{"last_tick": "garbage"}')
        with mock.patch.object(heal, "_launch", side_effect=OSError("no fork")):
            self.assertFalse(heal.maybe_start())


class Wiring(HookRig, unittest.TestCase):
    def setUp(self):
        self.rig()
        self.addCleanup(self.unrig)

    def test_hook_starts_a_tick_and_depth_does_not(self):
        launched = []
        with mock.patch.object(heal, "_launch", lambda: launched.append(1)):
            self.run_hook("claude", "turn-start", self.claude("UserPromptSubmit", prompt="hi"))
            self.assertEqual(len(launched), 1)
            state.path("health.json").unlink()
            with mock.patch.dict(os.environ, {"TAGTEAM_DEPTH": "1"}):
                self.run_hook("claude", "turn-start", self.claude("UserPromptSubmit", prompt="hi"))
            self.assertEqual(len(launched), 1)

    def test_helpers_stub_the_launch(self):
        self.assertNotEqual(heal._launch.__module__, "tagteam.heal")

    def test_doctor_fix_reports_a_config_error_without_a_traceback(self):
        from tagteam import cli
        write_user_config("[[[")
        with mock.patch.object(heal, "tick") as t, self.assertRaises(SystemExit) as e:
            cli.main(["doctor", "--fix"])
        self.assertTrue(str(e.exception.code).startswith("config error: "), e.exception.code)
        t.assert_not_called()

    def test_heal_command_runs_a_tick(self):
        from tagteam import cli
        with mock.patch.object(heal, "tick") as t:
            cli.main(["heal"])
        t.assert_called_once()
        with mock.patch.object(heal, "tick", side_effect=RuntimeError("boom")):
            cli.main(["heal"])                          # logged, not raised
        self.assertIn("heal tick error", state.path("tagteam.log").read_text())


if __name__ == "__main__":
    unittest.main()
