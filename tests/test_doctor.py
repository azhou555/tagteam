import contextlib
import dataclasses
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.helpers import ALL_AGENTS, clear_user_config, use_toml, write_user_config  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import config, doctor, harnesses, limits  # noqa: E402
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
        for trust, mark in ((False, "⚠ approve hooks in codex (/hooks)"), (None, "? approve hooks in codex (/hooks)"),
                            (True, "")):
            h = harnesses.ALL["codex"]
            with mock.patch.dict(harnesses.ALL, {"codex": dataclasses.replace(h, hook_trust=lambda t=trust: t)}):
                rc, out = self.run_doctor()
            self.assertEqual(rc, 0)
            self.assertEqual("approve hooks" in self.line(out, "codex"), bool(mark))
            if mark:
                self.assertIn(mark, self.line(out, "codex"))

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


if __name__ == "__main__":
    unittest.main()
