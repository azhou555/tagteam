import contextlib
import dataclasses
import io
import json
import os
import pathlib
import pty
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.helpers import FIX, ROOT, USER_CONFIG, clear_user_config, state, write_user_config  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import cli, config, doctor, harnesses, install  # noqa: E402
from tagteam.harnesses import agy as agy_h, claude as claude_h, codex as codex_h, copilot as copilot_h  # noqa: E402



class Install(unittest.TestCase):
    def setUp(self):
        d = pathlib.Path(tempfile.mkdtemp())
        self.settings = d / "settings.json"
        patches = [(claude_h, "PATH", self.settings), (codex_h, "PATH", d / "codex.json"),
                   (copilot_h, "PATH", d / "copilot.json"), (agy_h, "PATH", d / "agy.json"),
                   (claude_h, "STATUSLINE_ORIG", d / "sl.orig"), (claude_h, "STATUSLINE_ORIG_REFRESH", d / "sl.refresh"),
                   (install, "SWIFTBAR_APP", d / "NoSwiftBar.app"), (install, "LAUNCHER", d / "tagteam.app"),
                   (install, "SWIFTBAR_DOMAIN", "invalid.tagteam-tests"), (install, "SWIFTBAR_DIR", d / "SwiftBar")]  # never the real plugin
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
            cmds = [c.args[0] for c in run.call_args_list]
            self.assertEqual((cmds[0], cmds[1][:3]), (["pgrep", "-qx", "SwiftBar"], ["open", "-g", "-a"]))  # folder kept: no restart
            self.assertIn(["open", "-g", install.SWIFTBAR_ENABLE], [c.args[0] for c in run.call_args_list])
            self.assertIn("<swiftbar.hideDisablePlugin>true", plugin.read_text())
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

    def test_no_macos_extras_leaves_menu_bar_launcher_and_status_line_alone(self):
        self.settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "ponytail"}}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TAGTEAM_NO_MACOS_EXTRAS": "1"}), contextlib.redirect_stdout(out), \
                mock.patch.object(install, "SWIFTBAR_APP", pathlib.Path(tempfile.mkdtemp())), \
                mock.patch.object(install, "install_launcher") as launcher, \
                mock.patch.object(install.subprocess, "run") as run:
            install.install(["claude"])
            install.uninstall()
        self.assertIn("menu bar: skipped", out.getvalue())
        launcher.assert_not_called()
        run.assert_not_called()
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"]["command"], "ponytail")

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

    def swiftbar_cmds(self, configured, running):
        """install_swiftbar's commands while SwiftBar is up for the first `running` pgrep checks."""
        home, checks = pathlib.Path(tempfile.mkdtemp()), iter(range(10 ** 6))

        def run(argv, **kw):
            return mock.Mock(returncode=0 if argv[0] == "pgrep" and next(checks) < running else 1)
        with contextlib.ExitStack() as st:
            for p in self.swiftbar_env(configured, home):
                st.enter_context(p)
            run = st.enter_context(mock.patch.object(install.subprocess, "run", side_effect=run))
            install.install_swiftbar()
        return [c.args[0][:3] for c in run.call_args_list if c.args[0][0] in ("pkill", "open")]

    def test_a_running_swiftbar_is_never_reopened(self):
        configured = pathlib.Path(tempfile.mkdtemp())
        self.assertEqual(self.swiftbar_cmds(configured, running=10 ** 6),
                         [["open", "-g", install.SWIFTBAR_ENABLE], ["open", "-g", "swiftbar://refreshallplugins"]])

    def test_a_restart_opens_swiftbar_only_once_it_has_quit(self):
        self.assertEqual(self.swiftbar_cmds(None, running=3)[:2], [["pkill", "-x", "SwiftBar"], ["open", "-g", "-a"]])
        self.assertEqual(self.swiftbar_cmds(None, running=10 ** 6)[:2],
                         [["pkill", "-x", "SwiftBar"], ["open", "-g", install.SWIFTBAR_ENABLE]])   # never quit: no reopen

    def relaunch(self, quits_after):
        """Run the launcher's shell with stand-in pkill/pgrep/open; pgrep says running for `quits_after` checks."""
        d = pathlib.Path(tempfile.mkdtemp())
        for name, body in (("pkill", "exit 0"), ("sleep", "exit 0"),
                           ("pgrep", f'n=$(cat {d}/n 2>/dev/null || echo 0); echo $((n+1)) > {d}/n; [ $n -lt {quits_after} ]'),
                           ("open", f'echo "$*" >> {d}/opened')):
            (d / name).write_text(f"#!/bin/sh\n{body}\n")
            (d / name).chmod(0o755)
        subprocess.run(["sh", "-c", install.relaunch_sh()], env={**os.environ, "PATH": f"{d}:/usr/bin:/bin"}, check=True)
        return (d / "opened").read_text().splitlines()

    def test_launcher_waits_for_swiftbar_to_quit_and_never_reopens_a_running_one(self):
        self.assertEqual(self.relaunch(3), [f"-g -a {install.SWIFTBAR_APP}", f"-g {install.SWIFTBAR_ENABLE}"])
        self.assertEqual(self.relaunch(10 ** 6), [f"-g {install.SWIFTBAR_ENABLE}"])

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

    @unittest.skipUnless(sys.platform == "darwin", "osacompile is macOS-only")
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
            script = real_run(["osadecompile", str(launcher)], capture_output=True, text=True).stdout
            self.assertIn(f"open -g '{install.SWIFTBAR_ENABLE}'", script)
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
        old = {"type": "command", "command": f"sh '{ROOT / 'bin' / 'tagteam-statusline'}'"}
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
        self.assertEqual(hooks["Stop"][1]["hooks"][0]["timeout"], 540)    # waits up to 240s, then reviews in 240s

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


class Tty(io.StringIO):
    def isatty(self):
        return True


class Guided(unittest.TestCase):
    """`tagteam install` as a user runs it: intro, choose, summary, confirm, apply, next steps."""
    USER_HOOK = {"hooks": [{"type": "command", "command": "my-own-hook"}]}
    tearDown = Install.tearDown

    def setUp(self):
        Install.setUp(self)                                  # the same isolation, without Install's tests
        clear_user_config()
        self.addCleanup(clear_user_config)
        self.settings.write_text(json.dumps({"hooks": {"Stop": [self.USER_HOOK]}}, indent=2) + "\n")
        self.before = self.settings.read_bytes()
        for p in (mock.patch.object(harnesses, "detected", return_value=["claude", "codex"]),
                  mock.patch("tagteam.doctor.run", return_value=0)):
            p.start()
            self.addCleanup(p.stop)

    def run_install(self, answers="", reviewers=("codex",), yes=False, dry_run=False, menubar=True, tty=False):
        out = (Tty if tty else io.StringIO)()
        with mock.patch("sys.stdin", (Tty if tty else io.StringIO)(answers)), contextlib.redirect_stdout(out):
            rc = install.run(["claude"], None if reviewers is None else list(reviewers), None, yes, dry_run, menubar)
        return rc, out.getvalue()

    def assert_nothing_written(self):
        self.assertEqual(self.settings.read_bytes(), self.before)
        self.assertFalse(USER_CONFIG.exists())

    def test_dry_run_shows_summary_and_diff_and_writes_nothing(self):
        rc, shown = self.run_install(dry_run=True)
        self.assertEqual(rc, 0)
        self.assert_nothing_written()
        self.assertIn("a different agent reviews the change", shown)
        self.assertIn("add tagteam's hooks; show tagteam in your status line (your own command still runs)", shown)
        self.assertIn("reviewers.use = codex", shown)
        self.assertIn('+            "command": ": tagteam-hook-form=1; /opt/bin/tagteam hook claude turn-end",', shown)
        self.assertNotIn('+      "hooks": [{"type": "command", "command": "my-own-hook"', shown)   # a diff, not the file
        self.assertIn("(dry run: nothing was written)", shown)

    def test_declining_the_confirm_writes_nothing(self):
        rc, shown = self.run_install("n\n")
        self.assertEqual(rc, 1)
        self.assert_nothing_written()
        self.assertIn("Apply? [Y/n]", shown)

    def test_ctrl_c_at_the_picker_writes_nothing(self):
        with mock.patch.object(install, "ask_consent", side_effect=KeyboardInterrupt):
            rc, shown = self.run_install(reviewers=None)
        self.assertEqual(rc, 130)
        self.assert_nothing_written()
        self.assertIn("cancelled: nothing was changed", shown)

    def test_confirm_applies_then_says_what_next(self):
        rc, shown = self.run_install("\n")
        self.assertEqual(rc, 0)
        data = json.loads(self.settings.read_text())
        self.assertIn(self.USER_HOOK, data["hooks"]["Stop"])
        self.assertIn("/opt/bin/tagteam hook claude turn-end", self.settings.read_text())
        self.assertEqual(config.load()["reviewers"]["use"], ["codex"])
        self.assertRegex(shown, r"updated .*settings\.json \(backup: settings\.json\.tagteam-bak-\d{14}\)")
        self.assertIn("Try it: ask claude for a change of 5+ lines. When its turn ends, codex reviews it", shown)
        doctor.run.assert_called_once_with(False, False)
        after = self.settings.read_bytes()
        rc, shown = self.run_install(yes=True, reviewers=None)                   # a re-run changes nothing
        self.assertIn("already up to date", shown)
        self.assertNotIn("updated", shown)
        self.assertEqual(self.settings.read_bytes(), after)
        self.assertIn("reviewers.use stays codex", shown)

    def test_a_terminal_gets_colors_and_no_color_turns_them_off(self):
        p = {"files": {"claude": (self.settings, self.settings.read_text(), self.settings.read_text())},
             "routing": False, "menubar": "write ~/SwiftBar/tagteam.2s.sh"}
        shown = install.summary(p, ["agy", "codex"], None, install.live.Paint(True))
        self.assertIn("\x1b[38;5;111magy\x1b[0m, \x1b[38;5;151mcodex\x1b[0m", shown)    # each agent its own color
        self.assertIn("\x1b[2malready up to date\x1b[0m", shown)                         # unchanged is dimmed
        self.assertIn(f"\x1b[38;5;{install.CHANGE}mwrite ~/SwiftBar", shown)              # a change stands out
        self.assertNotIn("\x1b", install.summary(p, ["agy"], None))                       # plain by default
        with mock.patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(install._paint().on)
            with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertFalse(install._paint().on)

    def test_codex_reminder_follows_codex_trust_not_this_run(self):
        p = {"files": {"codex": (self.settings, "same", "same")}}                    # unchanged by this run
        for trust, shown in (("stale", True), ("unapproved", True), (None, True), ("trusted", False)):
            h = dataclasses.replace(harnesses.ALL["codex"], hook_trust=lambda t=trust: t)
            out = io.StringIO()
            with mock.patch.dict(harnesses.ALL, {"codex": h}), contextlib.redirect_stdout(out):
                install.next_steps(p)
            self.assertEqual("run /hooks, and approve" in out.getvalue(), shown, trust)

    def test_one_agent_stops_before_any_prompt(self):
        with mock.patch.object(harnesses, "detected", return_value=["claude"]), \
                mock.patch.object(install, "ask_consent", side_effect=AssertionError("prompted")):
            rc, shown = self.run_install(reviewers=None)
        self.assertEqual(rc, 1)
        self.assert_nothing_written()
        self.assertIn("tagteam needs two agents", shown)

    def test_a_menu_bar_failure_keeps_the_hooks(self):
        with mock.patch.object(install, "install_swiftbar", side_effect=OSError("disk full")):
            rc, shown = self.run_install(yes=True)
        self.assertEqual(rc, 0)
        self.assertIn("/opt/bin/tagteam hook claude", self.settings.read_text())
        self.assertIn("menu bar: failed (disk full). Hooks are in place", shown)

    def test_no_menubar_leaves_the_menu_bar_alone(self):
        with mock.patch.object(install, "install_swiftbar") as bar:
            rc, shown = self.run_install(yes=True, menubar=False)
        self.assertEqual(rc, 0)
        bar.assert_not_called()
        self.assertIn("/opt/bin/tagteam hook claude", self.settings.read_text())
        self.assertIn("skipped (--no-menubar): SwiftBar and the launcher are left as they are", shown)

    def test_summary_says_whose_turns_are_reviewed(self):
        shown = self.run_install(dry_run=True)[1]
        self.assertIn("authors.review stays claude, codex, copilot, agy", shown)
        same = install.summary({"files": {}, "routing": False, "menubar": "x"}, None, ["claude", "codex", "copilot", "agy"])
        self.assertIn("authors.review stays", same)                  # --authors with the saved list changes nothing

    def pick_authors(self, picked, current=None, agents=("claude", "codex")):
        if current is not None:
            write_user_config(f"[authors]\nreview = {json.dumps(current)}\n")
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch.object(install, "_pick", return_value=picked) as pick:
            got = install.choose_authors(None, False, list(agents))
        return got, pick.call_args.args if pick.called else None

    def test_authors_picker(self):
        got, shown = self.pick_authors(["claude", "codex"])
        self.assertIsNone(got)                                       # unchanged: nothing to save
        self.assertEqual(shown, (["claude", "codex"], ["claude", "codex"], {}))
        got, _ = self.pick_authors(["claude"])
        self.assertEqual(got, ["claude", "copilot", "agy"])          # agents not wired now keep their setting
        got, shown = self.pick_authors(["codex"], current=["claude", "agy"])
        self.assertEqual(shown[1], ["claude"])                       # preselects the saved list
        self.assertEqual(got, ["codex", "agy"])
        got, _ = self.pick_authors([], current=["claude"])
        self.assertEqual(got, [])

    def test_install_saves_the_authors_picked(self):
        with mock.patch.object(install, "_pick", return_value=[]), mock.patch.object(install, "install_swiftbar"):
            rc, shown = self.run_install("y\n", tty=True)
        self.assertEqual(rc, 0)
        self.assertEqual(config.load()["authors"]["review"], ["codex", "copilot", "agy"])   # claude unpicked
        self.assertIn("authors.review = codex, copilot, agy", re.sub(r"\x1b\[[0-9;]*m", "", shown))

    def test_authors_picker_only_on_a_terminal_and_without_flags(self):
        with mock.patch.object(install, "_pick", side_effect=AssertionError("prompted")):
            self.assertIsNone(install.choose_authors(None, False, ["claude"]))      # stdin is not a terminal
            with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
                self.assertIsNone(install.choose_authors(None, True, ["claude"]))
                self.assertEqual(install.choose_authors(["codex"], False, ["claude"]), ["codex"])

    def test_a_hook_write_failure_names_the_backups_and_exits_1(self):
        def failing_save(p, data, dry_run, real=harnesses.base.save):
            if not dry_run:
                raise OSError("read-only file system")
            return real(p, data, dry_run)
        with mock.patch.object(harnesses.base, "save", side_effect=failing_save):
            rc, shown = self.run_install(yes=True)
        self.assertEqual(rc, 1)
        self.assert_nothing_written()
        self.assertIn("install stopped: read-only file system", shown)
        self.assertIn("run `tagteam install` again", shown)


class Consent(unittest.TestCase):
    def setUp(self):
        clear_user_config()
        p = mock.patch.object(harnesses, "detected", return_value=["codex", "copilot"])
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(clear_user_config)

    def ask(self, text, detected=("codex", "copilot"), preselected=()):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(text)), contextlib.redirect_stdout(out):
            got = install.ask_consent(list(detected), list(preselected))
        return got, out.getvalue()

    def consent(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            install.set_consent(**{"reviewers": None, "authors": None, "yes": False, **kw})
        return out.getvalue()

    def test_picker_keys_move_select_and_number_in_pick_order(self):
        state = (0, [])
        for key in ("toggle", "down", "down", "toggle", "down", "toggle", "toggle", "up", None):
            state = install.pick_step(3, *state, key)
        self.assertEqual(state, (2, [2, 0]))       # down wraps 2 -> 0, up wraps 0 -> 2; re-picking moves to the end

    def pty_pick(self, args, keys):
        """The picker's whole output on a real terminal, after typing `keys`."""
        pid, fd = pty.fork()
        if pid == 0:                                         # child: the picker on the pty
            try:
                print(install._pick(*args), flush=True)
            finally:
                os._exit(0)
        for k in keys:
            time.sleep(0.3)
            os.write(fd, k.encode())
        out = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        os.waitpid(pid, 0)
        return out.decode().replace("\x1b[2K", "").replace("\x1b[?25l", "").replace("\x1b[?25h", "")

    def test_picker_on_a_real_terminal(self):
        text = self.pty_pick((["codex", "copilot", "agy"], ["copilot"], {"codex": "model x"}),
                             (" ", "\x1b[B\x1b[B", " ", "\n"))      # pick codex, move to agy, pick it, save
        self.assertIn("\x1b[38;5;151mcodex   \x1b[0m", text)                  # a terminal gets each agent's color
        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
        self.assertIn("› [2] codex   model x", text)
        self.assertEqual(text.strip().splitlines()[-1].strip(), "['copilot', 'codex', 'agy']")

    def test_authors_picker_marks_instead_of_numbering(self):
        text = re.sub(r"\x1b\[[0-9;]*m", "", self.pty_pick((["claude", "codex"], ["claude", "codex"], {}, False),
                                                           ("\x1b[B", " ", "\n")))   # unpick codex, save
        self.assertIn("whose turns get reviewed", text)
        self.assertIn("› [x] codex", text)
        self.assertNotIn("[1]", text)
        self.assertEqual(text.strip().splitlines()[-1].strip(), "['claude']")

    def test_install_preselects_the_current_list(self):
        write_user_config('[reviewers]\nuse = ["copilot", "kiro"]\n')
        with mock.patch.object(install, "ask_consent", return_value=["copilot"]) as ask:
            self.consent()
        self.assertEqual(ask.call_args.args[:2], (["codex", "copilot"], ["copilot"]))
        self.assertIn("model", ask.call_args.args[2]["codex"])                   # each choice carries its note

    def test_deselecting_a_saved_list_at_the_prompt_clears_it(self):
        write_user_config('[reviewers]\nuse = ["copilot"]\n')
        with mock.patch.object(install, "ask_consent", return_value=[]):
            shown = self.consent()
        self.assertEqual((config.load()["reviewers"]["use"], "saved reviewers.use = (none): nothing is reviewed" in shown),
                         ([], True))
        with mock.patch.object(install, "ask_consent", side_effect=AssertionError("prompted")):
            self.consent(yes=True)                           # --yes alone leaves the saved list as it is
        self.assertEqual(config.load()["reviewers"]["use"], [])

    def test_an_empty_saved_list_preselects_nothing_not_legacy(self):
        write_user_config('[reviewers]\nuse = []\n[agents.codex]\nenabled = true\n')
        with mock.patch.object(install, "ask_consent", return_value=[]) as ask:
            self.consent()
        self.assertEqual(ask.call_args.args[:2], (["codex", "copilot"], []))

    def test_ask_orders_choices_and_shows_copy(self):
        got, shown = self.ask("2,1\n")
        self.assertEqual(got, ["copilot", "codex"])
        self.assertIn("These agents can review your changes. Each one you pick receives your diffs. "
                      "Pick reviewers in order (e.g. 2,1), or press Enter for none:", shown)
        self.assertNotIn("[x]", shown)

    def test_ask_enter_is_none_and_bad_input_reprompts(self):
        self.assertEqual(self.ask("\n")[0], [])
        self.assertEqual(self.ask("")[0], [])
        self.assertEqual(self.ask("9,x\n1\n")[0], ["codex"])
        self.assertEqual(self.ask("1,1,2\n")[0], ["codex", "copilot"])

    def test_preselected_marked_with_x(self):
        _, shown = self.ask("\n", preselected=["codex"])
        [codex_line] = [ln for ln in shown.splitlines() if "codex" in ln and ln.strip().startswith("1")]
        [copilot_line] = [ln for ln in shown.splitlines() if "copilot" in ln and ln.strip().startswith("2")]
        self.assertIn("[x]", codex_line)
        self.assertNotIn("[x]", copilot_line)

    def test_interactive_saves_ordered_use(self):
        with mock.patch("sys.stdin", io.StringIO("2,1\n")):
            self.consent()
        self.assertEqual(config.load()["reviewers"]["use"], ["copilot", "codex"])

    def test_yes_alone_saves_no_use_and_says_so(self):
        with mock.patch.object(install, "ask_consent", side_effect=AssertionError("prompted")):
            shown = self.consent(yes=True)
        self.assertNotIn("use", config.load()["reviewers"])
        self.assertIn("no reviewer list saved", shown)
        self.assertIn("--reviewers", shown)

    def test_reviewers_flag_is_the_consent_without_prompt(self):
        with mock.patch.object(install, "ask_consent", side_effect=AssertionError("prompted")):
            self.consent(reviewers=["codex"])
        self.assertEqual(config.load()["reviewers"]["use"], ["codex"])

    def test_legacy_enabled_preselects_but_is_never_saved_as_use(self):
        write_user_config('[agents.codex]\nenabled = true\n[agents.copilot]\nenabled = false\n')
        with mock.patch.object(install, "ask_consent", return_value=[]) as ask:
            self.consent()
        self.assertEqual(ask.call_args.args[:2], (["codex", "copilot"], ["codex"]))
        self.assertNotIn("use", config.load()["reviewers"])

    def test_save_keeps_other_settings_and_replaces_old_list(self):
        write_user_config('[limits]\nmax_reviews_per_hour = 7\n\n[reviewers]\nuse = ["agy"]\n'
                          '[reviewers.codex]\nmodel = "m"\n')
        self.consent(reviewers=["codex", "copilot"], authors=["claude"])
        cfg = config.load()
        self.assertEqual((cfg["reviewers"]["use"], cfg["reviewers"]["codex"]["model"], cfg["authors"]["review"],
                          cfg["limits"]["max_reviews_per_hour"]), (["codex", "copilot"], "m", ["claude"], 7))

    def test_dry_run_writes_nothing(self):
        self.consent(reviewers=["codex"], dry_run=True)
        self.assertFalse(USER_CONFIG.exists())

    def test_no_detected_agents_saves_nothing(self):
        with mock.patch.object(harnesses, "detected", return_value=[]):
            shown = self.consent()
        self.assertFalse(USER_CONFIG.exists())
        self.assertIn("no reviewer list saved", shown)

    def test_cli_passes_flags(self):
        with mock.patch.object(install, "run", return_value=0) as run, contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(harnesses, "detected", return_value=["claude", "codex"]), self.assertRaises(SystemExit):
            cli.main(["install", "--reviewers", "codex,copilot", "--authors", "claude", "--yes"])
        run.assert_called_once_with(["claude", "codex"], ["codex", "copilot"], ["claude"], True, False, True)
        with mock.patch.object(install, "run") as run, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            cli.main(["install", "--reviewers", "kiro"])
        run.assert_not_called()
        with mock.patch.object(install, "run", return_value=0) as run, self.assertRaises(SystemExit):
            cli.main(["install", "--yes", "--agents", "copilot"])
        self.assertEqual(run.call_args.args[0], ["copilot"])
        with mock.patch.object(install, "run", return_value=0) as run, self.assertRaises(SystemExit):
            cli.main(["install", "--yes", "--no-menubar"])
        self.assertIs(run.call_args.args[5], False)

    def test_bare_command_and_help_say_what_tagteam_is(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main([])
        self.assertIn("a different agent reviews the change", out.getvalue())
        self.assertIn("Start with `tagteam install`", out.getvalue())

    def migrate(self, text):
        if text is None:
            clear_user_config()
        else:
            write_user_config(text)
        with mock.patch("builtins.print"), mock.patch.object(install, "check_agent", return_value=0), \
                mock.patch.object(install, "install_swiftbar"):
            install.install([])
        return config.load()["routing"]["mode"], USER_CONFIG.read_text() if USER_CONFIG.exists() else None

    def test_existing_old_config_keeps_quota_routing(self):
        mode, text = self.migrate('[agents.codex]\nenabled = true\n[routing]\nsmall = 2\n')
        self.assertEqual(mode, "quota")
        self.assertEqual(self.migrate(text)[0], "quota")                            # idempotent
        self.assertEqual(text.count("[routing]"), 1)
        self.assertEqual(self.migrate("# mine\n")[0], "quota")

    def test_fresh_or_new_style_config_stays_ordered(self):
        self.assertEqual(self.migrate(None), ("ordered", None))
        self.assertEqual(self.migrate('[reviewers]\nuse = ["codex"]\n')[0], "ordered")   # written by a new install
        self.assertEqual(self.migrate('[routing]\nmode = "ordered"\n')[0], "ordered")

    def test_extras_off_only_for_truthy_values(self):
        for v, want in (("1", True), ("true", True), ("yes", True), ("0", False), ("", False), ("no", False)):
            with mock.patch.dict(os.environ, {"TAGTEAM_NO_MACOS_EXTRAS": v}):
                self.assertIs(state.extras_off(), want, v)

    def test_cli_config_error_is_a_message_not_a_traceback(self):
        write_user_config('[reviewers]\nuse = ["a"\n')
        err = io.StringIO()
        with mock.patch.object(install, "install"), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["install", "--reviewers", "codex", "--yes"])
        self.assertNotIn(cm.exception.code, (0, None))
        self.assertIn("config error", str(cm.exception.code) + err.getvalue())
        self.assertIn("config.toml", str(cm.exception.code) + err.getvalue())

    def test_unrewritable_use_is_a_config_error(self):
        write_user_config('[reviewers]\nuse = "codex"\n')
        with self.assertRaises(config.ConfigError):
            self.consent(reviewers=["codex"])


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

    def test_old_statusline_shim_runs_the_interpreter_it_was_given(self):
        home, d = tempfile.mkdtemp(), tempfile.mkdtemp()
        shim = str(ROOT / "bin" / "tagteam-statusline")
        r = subprocess.run(["sh", shim, sys.executable], input='{"session_id": "s1"}', capture_output=True, text=True,
                           env={**os.environ, "TAGTEAM_HOME": home, "NO_COLOR": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("tagteam"), r.stdout)
        fake = pathlib.Path(d, "py")
        fake.write_text(f"#!/bin/sh\necho \"$@\" > {d}/argv\n")
        fake.chmod(0o755)
        subprocess.run(["sh", shim, str(fake)], input=b"{}", capture_output=True)
        self.assertEqual(pathlib.Path(d, "argv").read_text().split(), [str(ROOT / "bin" / "tagteam"), "statusline"])

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
