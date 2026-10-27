import contextlib
import dataclasses
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.helpers import ALL_AGENTS, clear_user_config, use_toml, write_user_config  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import config, doctor, harnesses, limits, quota, state  # noqa: E402
from tagteam.harnesses import codex as codex_h  # noqa: E402
from tagteam.harnesses.base import TAG  # noqa: E402


class Doctor(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.entry = self.d / "tagteam"
        self.entry.write_text("")
        self.paths = {n: self.d / f"{n}.json" for n in ALL_AGENTS}
        self.logged_in = {n: True for n in ALL_AGENTS}
        self.addCleanup(clear_user_config)
        self.detected = list(ALL_AGENTS)
        self.ps = [mock.patch.object(harnesses, "detected", side_effect=lambda: list(self.detected)),
                   mock.patch.object(harnesses, "ALL", {n: dataclasses.replace(
                       h, hooks_path=lambda n=n: self.paths[n], logged_in=lambda n=n: self.logged_in[n],
                       hook_trust=None) for n, h in harnesses.ALL.items()})]
        for p in self.ps:
            p.start()
            self.addCleanup(p.stop)

    def hooks(self, name, entry=None):
        cmd = f": {TAG}; {entry or self.entry} hook {name} turn-end"
        self.paths[name].write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": cmd}]}]}}))

    def run_doctor(self, probe=False, clear=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = doctor.run(probe, clear)
        return rc, out.getvalue()

    def line(self, out, name):
        return next(l for l in out.splitlines() if l.startswith(name))

    def test_missing_entry_path_flagged(self):
        self.hooks("claude", entry=self.d / "gone" / "tagteam")
        rc, out = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("✗", self.line(out, "claude"))
        self.assertIn("gone", self.line(out, "claude"))

    def test_unlisted_reviewer_reported_not_error(self):
        self.hooks("claude")
        write_user_config(use_toml(["codex"]))
        rc, out = self.run_doctor()
        self.assertIn("reviewer – not listed", self.line(out, "claude"))
        self.assertIn("reviewer ✓ listed #1", self.line(out, "codex"))
        self.assertEqual(rc, 0)

    def test_disabled_reviewer_and_clear(self):
        write_user_config(use_toml(["codex"]))
        limits.disable("codex", "wrote files")
        rc, out = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("disabled", self.line(out, "codex"))
        self.assertIn("tagteam doctor --clear", self.line(out, "codex"))
        rc, out = self.run_doctor(clear=True)
        self.assertEqual(limits.disabled(), {})
        self.assertEqual(rc, 0)
        self.assertIn("reviewer ✓ listed #1", self.line(out, "codex"))

    def test_exit_code_reflects_configured_roles_only(self):
        self.logged_in["agy"] = False                       # not configured: no hooks, not listed
        self.detected.remove("copilot")
        rc, out = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("not installed", self.line(out, "copilot"))
        write_user_config(use_toml(["agy"]))
        rc, out = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("not logged in", self.line(out, "agy"))

    def test_codex_trust_hint_never_fails(self):
        self.hooks("codex")
        for trust, want in (("unapproved", "⚠ approve hooks in codex (/hooks)"),
                            ("stale", "⚠ re-approve hooks in codex (/hooks)"),
                            (None, "? approve hooks in codex (/hooks)"), ("trusted", "")):
            h = harnesses.ALL["codex"]
            with mock.patch.dict(harnesses.ALL, {"codex": dataclasses.replace(h, hook_trust=lambda t=trust: t)}):
                rc, out = self.run_doctor()
            self.assertEqual(rc, 0)
            line = self.line(out, "codex")
            self.assertEqual("approve hooks" in line, bool(want))
            self.assertIn(want, line)

    def test_clear_says_whether_it_cleared(self):
        _, out = self.run_doctor(clear=True)
        self.assertIn("no disabled reviewers to clear", out)
        limits.disable("codex", "wrote files")
        _, out = self.run_doctor(clear=True)
        self.assertIn("cleared disabled reviewers: codex", out)

    def test_paused_reviewer_shown_with_time_left_and_cleared(self):
        write_user_config(use_toml(["codex", "copilot"]))
        quota.cooldown("codex", 15)
        state.write_json(state.path("failures.json"), {"copilot": 1})
        rc, out = self.run_doctor()
        self.assertEqual(rc, 0)
        self.assertIn("paused 15 more min", self.line(out, "codex"))
        self.assertIn("tagteam doctor --clear", self.line(out, "codex"))
        self.assertIn("reviewer ✓ listed #2", self.line(out, "copilot"))
        rc, out = self.run_doctor(clear=True)
        self.assertIn("resumed paused reviewers: codex", out)
        self.assertIn("reviewer ✓ listed #1", self.line(out, "codex"))
        self.assertFalse(quota.cooling("codex"))
        self.assertEqual(state.read_json(state.path("failures.json")), {})

    def test_unreadable_safety_lists_reported_and_reset(self):
        write_user_config(use_toml(["codex"]))
        for name, said in (("disabled.json", "reset the unreadable disabled-reviewer list"),
                           ("cooldowns.json", "reset the unreadable cooldown list")):
            with self.subTest(name):
                state.path(name).write_text("[1, 2")
                rc, out = self.run_doctor()
                self.assertEqual(rc, 1)
                self.assertIn("unreadable", self.line(out, "codex"))
                self.assertIn("tagteam doctor --clear", self.line(out, "codex"))
                rc, out = self.run_doctor(clear=True)
                self.assertIn(said, out)
                self.assertEqual((rc, state.read_json(state.path(name))), (0, {}))
                self.assertIn("reviewer ✓ listed #1", self.line(out, "codex"))

    def test_probe_unknown_when_recheck_fails(self):
        good = json.dumps({"structured_output": {"verdict": "approve", "summary": "", "findings": []}})
        real = doctor.changes.snapshot
        calls = []

        def snap(path):
            calls.append(path)
            if len(calls) > 1:
                raise RuntimeError("boom")
            return real(path)
        with mock.patch.object(doctor.runner, "_spawn", return_value=(good, "", 0, False)), \
                mock.patch.object(doctor.changes, "snapshot", snap):
            self.assertTrue(doctor.probe_one("claude", config.load()).startswith("unknown"))


    def test_config_error_fails(self):
        write_user_config("[reviewers]\nuse = 3\n")
        rc, out = self.run_doctor()
        self.assertEqual(rc, 1)
        self.assertIn("config error", out)

    def test_probe_reports_each_listed_reviewer(self):
        write_user_config(use_toml(["codex", "claude"]))
        results = {"codex": "ok", "claude": "timeout"}
        with mock.patch.object(doctor, "probe_one", side_effect=lambda name, cfg: results[name]) as p:
            rc, out = self.run_doctor(probe=True)
        self.assertEqual([c.args[0] for c in p.call_args_list], ["codex", "claude"])
        self.assertIn("probe codex: ok", out)
        self.assertIn("probe claude: timeout", out)
        self.assertEqual(rc, 1)
        with mock.patch.object(doctor, "probe_one") as p:
            self.run_doctor()
        p.assert_not_called()

    def test_probe_one_outcomes(self):
        good = json.dumps({"structured_output": {"verdict": "approve", "summary": "", "findings": []}})

        def spawn(wrote, timed_out):
            def f(argv, stdin, cwd, timeout):
                if wrote:
                    (pathlib.Path(cwd) / "new.txt").write_text("x")
                return (good, "", 0, timed_out)
            return f
        for wrote, timed_out, want in ((False, False, "ok"), (True, False, "wrote-files"), (False, True, "timeout")):
            with mock.patch.object(doctor.runner, "_spawn", spawn(wrote, timed_out)):
                self.assertEqual(doctor.probe_one("claude", config.load()), want)


class CodexTrust(unittest.TestCase):
    def setUp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.hooks, self.cfg, self.snap = d / "hooks.json", d / "config.toml", d / "snap.json"
        self.hooks.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"command": "other"}]}],
            "UserPromptSubmit": [{"hooks": [{"command": "other"}]}, {"hooks": [{"command": f": {TAG}; x hook codex a"}]}],
            "PostToolUse": [{"hooks": [{"command": f": {TAG}; x hook codex b"}]}],
            "Stop": [{"hooks": [{"command": "other"}]}, {"hooks": [{"command": f": {TAG}; x hook codex c"}]}]}}))
        self.keys = [f"{self.hooks}:user_prompt_submit:1:0", f"{self.hooks}:post_tool_use:0:0", f"{self.hooks}:stop:1:0"]
        for m, k, v in ((codex_h, "PATH", self.hooks), (codex_h, "CONFIG", self.cfg),
                        (codex_h, "snapshot_path", lambda: self.snap)):
            p = mock.patch.object(m, k, v)
            p.start()
            self.addCleanup(p.stop)

    def records(self, hashes):
        self.cfg.write_text("[hooks.state]\n" + "".join(
            f'[hooks.state."{k}"]\nenabled = true\ntrusted_hash = "{h}"\n' for k, h in hashes.items()))

    def test_key_format_and_trust_states(self):
        self.assertEqual(set(codex_h._our_keys()), set(self.keys))
        self.records({self.keys[0]: "sha256:a", self.keys[1]: "sha256:b"})            # one hook has no record
        self.assertEqual(codex_h.hook_trust(), "unapproved")
        self.records({k: "sha256:old" for k in self.keys})
        self.assertIsNone(codex_h.hook_trust())                                      # no install snapshot
        codex_h.save_trust_snapshot()
        self.assertEqual(codex_h.hook_trust(), "stale")                               # approvals predate install
        self.records({k: "sha256:new" for k in self.keys})
        self.assertEqual(codex_h.hook_trust(), "trusted")                             # re-approved after install
        self.records({**{k: "sha256:old" for k in self.keys[:2]}, self.keys[2]: "sha256:new"})
        self.assertEqual(codex_h.hook_trust(), "stale")                               # one still stale

    def test_key_absent_at_install_counts_as_approved_later(self):
        self.records({})
        codex_h.save_trust_snapshot()
        self.assertEqual(json.loads(self.snap.read_text()), {})
        self.records({k: "sha256:x" for k in self.keys})
        self.assertEqual(codex_h.hook_trust(), "trusted")

    def test_install_saves_snapshot(self):
        self.records({k: "sha256:old" for k in self.keys})
        with contextlib.redirect_stdout(io.StringIO()):
            codex_h.install("/bin/tagteam", 100, dry_run=False)
        self.assertEqual(set(json.loads(self.snap.read_text())), set(codex_h._our_keys()))

    def test_unreadable_files(self):
        self.cfg.write_text("not [ toml")
        self.assertIsNone(codex_h.hook_trust())
        self.cfg.unlink()
        self.assertIsNone(codex_h.hook_trust())
        self.records({k: "sha256:old" for k in self.keys})
        self.hooks.write_text("{nope")
        self.assertIsNone(codex_h.hook_trust())
        self.hooks.unlink()
        self.assertIsNone(codex_h.hook_trust())


if __name__ == "__main__":
    unittest.main()
