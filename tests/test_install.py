import contextlib
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.helpers import FIX, state  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import harnesses, install  # noqa: E402
from tagteam.harnesses import agy as agy_h, claude as claude_h, codex as codex_h, copilot as copilot_h  # noqa: E402

ROOT = state.ROOT


class Install(unittest.TestCase):
    def setUp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.settings = d / "settings.json"
        patches = [(claude_h, "PATH", self.settings), (codex_h, "PATH", d / "codex.json"),
                   (copilot_h, "PATH", d / "copilot.json"), (agy_h, "PATH", d / "agy.json"),
                   (claude_h, "STATUSLINE_ORIG", d / "sl.orig"), (claude_h, "STATUSLINE_ORIG_REFRESH", d / "sl.refresh"),
                   (install, "SWIFTBAR_APP", d / "NoSwiftBar.app"), (install, "LAUNCHER", d / "tagteam.app")]
        self.ps = [mock.patch.object(m, k, v) for m, k, v in patches]
        self.ps += [mock.patch.object(install, "check_agent", return_value=0),
                    mock.patch.object(install, "entry_argv", return_value=["/opt/bin/tagteam"])]
        for x in self.ps:
            x.start()

    def tearDown(self):
        for x in self.ps:
            x.stop()

    def roundtrip(self, statusline):
        self.settings.write_text(json.dumps({"statusLine": statusline}))
        with mock.patch("builtins.print"):
            install.install(["claude"])
            first = json.loads(self.settings.read_text())["statusLine"]
            install.install(["claude"])                        # re-install over our own wrapper
            again = json.loads(self.settings.read_text())["statusLine"]
            install.uninstall()
        return first, again, json.loads(self.settings.read_text())["statusLine"]

    def test_refresh_added_and_removed(self):
        first, again, after = self.roundtrip({"type": "command", "command": "ponytail"})
        self.assertEqual(first["command"], "/opt/bin/tagteam statusline")
        self.assertEqual((first["refreshInterval"], again), (2, first))
        self.assertEqual(after, {"type": "command", "command": "ponytail"})

    def test_users_refresh_restored(self):
        first, _, after = self.roundtrip({"type": "command", "command": "ponytail", "refreshInterval": 10})
        self.assertEqual(first["refreshInterval"], 2)
        self.assertEqual(after, {"type": "command", "command": "ponytail", "refreshInterval": 10})

    def test_swiftbar_plugin_installed_runs_and_uninstalls(self):
        app, plugins = pathlib.Path(tempfile.mkdtemp()), pathlib.Path(tempfile.mkdtemp())
        plugin = plugins / install.SWIFTBAR_PLUGIN
        with mock.patch.object(install, "SWIFTBAR_APP", app), \
                mock.patch.object(install, "_swiftbar_dir", return_value=plugins), mock.patch("builtins.print"), \
                mock.patch.object(install, "install_launcher"), \
                mock.patch.object(install, "entry_argv", return_value=[str(ROOT / "bin" / "tagteam")]):
            with mock.patch.object(install.subprocess, "run") as run:
                install.install_swiftbar()
            self.assertEqual(run.call_args_list[0].args[0][:3], ["open", "-g", "-a"])   # folder kept: no restart
            self.assertTrue(os.access(plugin, os.X_OK))
            out = subprocess.run([str(plugin)], capture_output=True, text=True).stdout
            self.assertRegex(out.splitlines()[0], r"^:person\.2\.fill: :[a-z.]+: ")
            self.assertIn("Open live view (tagteam watch)", out)
            install.uninstall()
            self.assertFalse(plugin.exists())
            plugin.write_text("#!/bin/sh\necho mine\n")              # a user's plugin with the same name
            with mock.patch.object(install.subprocess, "run"):
                install.install_swiftbar()
            install.uninstall()
        self.assertEqual(plugin.read_text(), "#!/bin/sh\necho mine\n")
        plugin.write_bytes(b"\xff\xfe binary plugin")
        self.assertFalse(install._ours_plugin(plugin))
        with mock.patch.object(install.shutil, "which", return_value=None):
            self.assertIsNone(install._swiftbar_dir())                   # no `defaults`: SwiftBar is a no-op

    def test_swiftbar_folder_set_when_unset_or_hidden_legacy(self):
        app, home = pathlib.Path(tempfile.mkdtemp()), pathlib.Path(tempfile.mkdtemp())
        legacy = home / ".tagteam" / "swiftbar"
        legacy.mkdir(parents=True)
        for configured in (None, legacy):
            with mock.patch.object(install, "SWIFTBAR_APP", app), \
                    mock.patch.object(install, "SWIFTBAR_DIR", home / "SwiftBar"), \
                    mock.patch.object(install, "SWIFTBAR_OLD_DIR", legacy), \
                    mock.patch.object(install, "_swiftbar_dir", return_value=configured), \
                    mock.patch.object(install.time, "sleep"), mock.patch("builtins.print"), \
                    mock.patch.object(install, "install_launcher"), \
                    mock.patch.object(install.subprocess, "run") as run:
                (legacy / install.SWIFTBAR_PLUGIN).write_text(install.swiftbar_plugin())
                install.install_swiftbar()
            cmds = [c.args[0] for c in run.call_args_list]
            self.assertIn(["defaults", "write", install.SWIFTBAR_DOMAIN, "PluginDirectory", str(home / "SwiftBar")], cmds)
            self.assertIn(["pkill", "-x", "SwiftBar"], cmds)
            self.assertTrue((home / "SwiftBar" / install.SWIFTBAR_PLUGIN).exists())
        self.assertFalse((legacy / install.SWIFTBAR_PLUGIN).exists())

    def swiftbar_env(self, configured, home):
        return [mock.patch.object(install, "SWIFTBAR_APP", pathlib.Path(tempfile.mkdtemp())),
                mock.patch.object(install, "SWIFTBAR_DIR", home / "SwiftBar"),
                mock.patch.object(install, "SWIFTBAR_OLD_DIR", home / ".tagteam" / "swiftbar"),
                mock.patch.object(install, "_swiftbar_dir", return_value=configured),
                mock.patch.object(install.time, "sleep"), mock.patch("builtins.print"),
                mock.patch.object(install, "install_launcher")]

    def test_swiftbar_migration_keeps_old_setup_when_destination_is_taken(self):
        home = pathlib.Path(tempfile.mkdtemp())
        legacy = home / ".tagteam" / "swiftbar"
        legacy.mkdir(parents=True)
        (legacy / install.SWIFTBAR_PLUGIN).write_text(install.swiftbar_plugin())
        (home / "SwiftBar").mkdir()
        (home / "SwiftBar" / install.SWIFTBAR_PLUGIN).write_text("#!/bin/sh\necho theirs\n")
        with contextlib.ExitStack() as st:
            for p in self.swiftbar_env(legacy, home):
                st.enter_context(p)
            run = st.enter_context(mock.patch.object(install.subprocess, "run"))
            install.install_swiftbar()
        self.assertEqual(run.call_args_list, [])                          # SwiftBar not repointed or restarted
        self.assertTrue(install._ours_plugin(legacy / install.SWIFTBAR_PLUGIN))
        self.assertEqual((home / "SwiftBar" / install.SWIFTBAR_PLUGIN).read_text(), "#!/bin/sh\necho theirs\n")

    def test_swiftbar_write_failure_keeps_old_setup(self):
        home = pathlib.Path(tempfile.mkdtemp())
        legacy = home / ".tagteam" / "swiftbar"
        legacy.mkdir(parents=True)
        (legacy / install.SWIFTBAR_PLUGIN).write_text(install.swiftbar_plugin())
        with contextlib.ExitStack() as st:
            for p in self.swiftbar_env(legacy, home):
                st.enter_context(p)
            run = st.enter_context(mock.patch.object(install.subprocess, "run"))
            st.enter_context(mock.patch.object(pathlib.Path, "write_text", side_effect=OSError("disk full")))
            with self.assertRaises(OSError):
                install.install_swiftbar()
        self.assertEqual(run.call_args_list, [])
        self.assertTrue(install._ours_plugin(legacy / install.SWIFTBAR_PLUGIN))

    def test_swiftbar_users_hidden_folder_is_left_alone(self):
        home = pathlib.Path(tempfile.mkdtemp())
        theirs = home / ".config" / "swiftbar"
        with contextlib.ExitStack() as st:
            for p in self.swiftbar_env(theirs, home):
                st.enter_context(p)
            run = st.enter_context(mock.patch.object(install.subprocess, "run"))
            install.install_swiftbar()
        self.assertEqual(run.call_args_list, [])
        self.assertFalse(theirs.exists())
        self.assertFalse((home / "SwiftBar").exists())

    def test_launcher_built_owned_and_removed(self):
        apps = pathlib.Path(tempfile.mkdtemp())
        launcher = apps / "tagteam.app"
        real_run = subprocess.run

        def run(argv, **kw):                     # build the bundle for real; skip mdimport side effects
            return real_run(argv, **kw) if argv[0] == "osacompile" else mock.Mock(returncode=0)
        with mock.patch.object(install, "LAUNCHER", launcher), mock.patch("builtins.print"), \
                mock.patch.object(install, "_swiftbar_dir", return_value=None), \
                mock.patch.object(install.subprocess, "run", side_effect=run):
            install.install_launcher()
            self.assertTrue((launcher / "Contents" / "MacOS").is_dir())
            self.assertTrue((launcher / install.LAUNCHER_MARK).exists())
            install.install_launcher()                                    # idempotent rebuild
            install.uninstall()
            self.assertFalse(launcher.exists())
            (launcher / "Contents").mkdir(parents=True)                   # someone else's tagteam.app
            install.install_launcher()
            install.uninstall()
            self.assertTrue((launcher / "Contents").exists())
            self.assertFalse((launcher / install.LAUNCHER_MARK).exists())

    def test_upgrade_from_old_wrapper_keeps_original_command(self):
        claude_h.STATUSLINE_ORIG.write_text("ponytail")
        old = {"type": "command", "command": f"sh '{state.ROOT / 'bin' / 'tagteam-statusline'}'"}
        first, _, after = self.roundtrip(old)
        self.assertEqual(first["refreshInterval"], 2)
        self.assertEqual(after, {"type": "command", "command": "ponytail"})

    def test_plugin_and_hooks_call_the_entry(self):
        self.assertIn("exec /opt/bin/tagteam swiftbar\n", install.swiftbar_plugin())
        with mock.patch("builtins.print"):
            install.install(["claude"])
        cmds = [h["command"] for g in json.loads(self.settings.read_text())["hooks"].values() for e in g
                for h in e["hooks"]]
        self.assertEqual(len(cmds), 4)
        for c in cmds:
            self.assertRegex(c, r"^: tagteam-hook-form=1; /opt/bin/tagteam hook claude [a-z-]+$")

    def test_install_rewrites_old_checkout_hooks_by_tag(self):
        old = ": tagteam-hook-form=1; '/usr/bin/python3' '/old/repo/bin/tagteam' hook claude turn-end"
        theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine  # /old/repo/bin/tagteam"}]}
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": old}]},
                                                                theirs]}}))
        with mock.patch("builtins.print"):
            install.install(["claude"])
        hooks = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(hooks["Stop"][0], theirs)
        ours = [e for g in hooks.values() for e in g if claude_h.base.ours(e)]
        self.assertEqual(len(ours), 4)
        self.assertEqual(len([e for e in hooks["Stop"] if claude_h.base.ours(e)]), 1)
        self.assertNotIn("/old/repo", json.dumps(ours))
        self.assertIn("/opt/bin/tagteam hook claude turn-end", json.dumps(hooks["Stop"][1]))

    def test_failed_check_keeps_old_entries(self):
        codex = pathlib.Path(codex_h.PATH)
        old = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": ": tagteam-hook-form=1; old hook"}]}]}}
        codex.write_text(json.dumps(old))
        before = codex.read_bytes()
        with mock.patch.object(install, "check_agent", side_effect=lambda n: 1 if n == "codex" else 0), \
                mock.patch("builtins.print") as pr:
            install.install(["claude", "codex"])
        self.assertEqual(codex.read_bytes(), before)
        self.assertIn("tagteam-hook-form=1", self.settings.read_text())
        self.assertIn("/opt/bin/tagteam hook claude", self.settings.read_text())
        self.assertTrue(any("codex: kept existing hooks (check failed" in str(c.args[0]) for c in pr.call_args_list))


class Entry(unittest.TestCase):
    def test_check_agent_runs_the_real_parser(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        with mock.patch.object(install, "entry_argv", return_value=[sys.executable, "-m", "tagteam"]), \
                mock.patch.dict(os.environ, env):
            self.assertEqual({n: install.check_agent(n) for n in harnesses.ALL}, dict.fromkeys(harnesses.ALL, 0))
        with mock.patch.object(install, "entry_argv", return_value=["/nonexistent/tagteam"]), \
                mock.patch("sys.stderr"):
            self.assertEqual(install.check_agent("claude"), 1)

    def test_entry_never_points_at_an_unexecutable_path(self):
        script = pathlib.Path(tempfile.mkdtemp()) / "tagteam"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        with mock.patch.object(install.shutil, "which", return_value="/usr/local/bin/tagteam"):
            self.assertEqual(install.entry(), "/usr/local/bin/tagteam")
        with mock.patch.object(install.shutil, "which", return_value=None):
            with mock.patch.object(sys, "argv", [str(script)]):
                self.assertEqual(install.entry(), shlex.quote(os.path.realpath(script)))
            for argv0 in (str(ROOT / "tagteam" / "__main__.py"), "-m", "/no/such/file"):
                with mock.patch.object(sys, "argv", [argv0]):
                    self.assertEqual(install.entry(), f"{shlex.quote(sys.executable)} -m tagteam")

    def test_which_result_is_made_absolute(self):
        with mock.patch.object(install.shutil, "which", return_value="rel/tagteam"):
            self.assertEqual(install.entry_argv(), [os.path.abspath("rel/tagteam")])

    def test_odd_path_stays_one_word_and_still_recognised(self):
        odd = "/tmp/it's here/tagteam"
        with mock.patch.object(install, "entry_argv", return_value=[odd]):
            cmd = install.entry()
            hook = claude_h.base.hook_cmd(cmd, "claude", "turn-end")
            self.assertEqual(shlex.split(cmd), [odd])
            self.assertEqual(shlex.split(hook)[-4:], [odd, "hook", "claude", "turn-end"])
            self.assertTrue(claude_h.base.ours({"command": hook}))
            self.assertTrue(claude_h.ours_statusline(f"{cmd} statusline"))
        with mock.patch.object(install, "entry_argv", return_value=["/opt/my py/python", "-m", "tagteam"]):
            self.assertTrue(claude_h.ours_statusline(f"{install.entry()} statusline"))


class Cli(unittest.TestCase):
    def run_tagteam(self, *args, stdin="", home=None, env=None):
        e = {**os.environ, "TAGTEAM_HOME": home or tempfile.mkdtemp(), "NO_COLOR": "1", **(env or {})}
        return subprocess.run([sys.executable, "-m", "tagteam", *args], input=stdin, capture_output=True, text=True,
                              cwd=ROOT, env=e)

    def test_check_mode_fails_closed(self):
        bad = self.run_tagteam("hook", "claude", "turn-end", "--check", stdin="not json")
        self.assertEqual((bad.returncode, bad.stdout), (1, ""))
        self.assertIn("claude turn-end", bad.stderr)
        ok = self.run_tagteam("hook", "claude", "turn-end", "--check", stdin=(FIX.parent.parent / "tagteam" / "data" /
                              "fixtures" / "claude-stop.json").read_text())
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "ok claude turn-end"))
        wrong = self.run_tagteam("hook", "codex", "turn-end", "--check", stdin=(ROOT / "tagteam" / "data" /
                                 "fixtures" / "claude-stop.json").read_text())
        self.assertEqual(wrong.returncode, 1)                           # a Claude payload is not a Codex event

    def test_check_mode_writes_no_state(self):
        home = tempfile.mkdtemp()
        for agent in harnesses.ALL:
            fixture = (ROOT / "tagteam" / "data" / "fixtures" / f"{agent}-stop.json").read_text()
            r = self.run_tagteam("hook", agent, "turn-end", "--check", stdin=fixture, home=home,
                                 env={"TAGTEAM": "off"})
            self.assertEqual((r.returncode, r.stdout.strip()), (0, f"ok {agent} turn-end"), r.stderr)
        self.assertEqual(list(pathlib.Path(home, "state", "sessions").glob("*")), [])
        self.assertEqual([p.name for p in pathlib.Path(home).rglob("*") if p.is_file()], [])

    def test_statusline_runs_original_saves_quota_and_adds_ours(self):
        home = tempfile.mkdtemp()
        pathlib.Path(home, "statusline.orig").write_text("echo orig-line; echo '' ")
        payload = json.dumps({"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 5}}})
        r = self.run_tagteam("statusline", stdin=payload, home=home)
        self.assertEqual(r.returncode, 0)
        lines = r.stdout.split("\n")
        self.assertEqual(lines[0], "orig-line")
        self.assertTrue(lines[1].startswith("tagteam"), r.stdout)
        self.assertEqual(pathlib.Path(home, "state", "quota", "claude.json").read_text(), payload)
        self.assertEqual(self.run_tagteam("statusline", stdin="not json", home=home).returncode, 0)

    def test_statusline_survives_quota_write_failure_and_bad_bytes(self):
        home = tempfile.mkdtemp()
        pathlib.Path(home, "statusline.orig").write_text("echo orig-line")
        pathlib.Path(home, "state").mkdir()
        pathlib.Path(home, "state", "quota").write_text("a file, so the quota dir cannot be made")
        r = subprocess.run([sys.executable, "-m", "tagteam", "statusline"], input=b"\xff\xfe{", capture_output=True,
                           cwd=ROOT, env={**os.environ, "TAGTEAM_HOME": home, "NO_COLOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(b"\n")[0], b"orig-line")
        self.assertEqual(r.stderr, b"")

    def test_swiftbar_subcommand_prints_menu(self):
        r = self.run_tagteam("swiftbar")
        self.assertTrue(r.stdout.startswith(":person.2.fill:"), r.stdout)


if __name__ == "__main__":
    unittest.main()
