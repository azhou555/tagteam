import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from tests.helpers import make_repo  # isort: skip (sets TAGTEAM_HOME before tagteam imports)
from tagteam import cli, config, runner, state  # isort: skip


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp())
        self.repo = pathlib.Path(tempfile.mkdtemp())
        for name, val in (("HOME", self.home), ("STATE", self.home / "state")):
            p = mock.patch.object(state, name, val)
            p.start()
            self.addCleanup(p.stop)

    def user(self, text):
        (self.home / "config.toml").write_text(text)

    def repo_file(self, text):
        (self.repo / ".tagteam.toml").write_text(text)

    def log_msgs(self):
        return [r["msg"] for r in state.read_jsonl(state.path("tagteam.log"))]


class Load(ConfigCase):
    def test_legacy_keys_map(self):
        self.user('[review]\nmax_rounds = 3\n[routing]\nsmall = 2\nlarge_lines = 99\ncooldown_min = 7\n'
                  'primary_use = "never"\n[agents.codex]\nmodel = "x"\ntier = "reviewer"\n')
        cfg = config.load()
        self.assertEqual(cfg["limits"]["max_rounds_per_turn"], 3)
        self.assertEqual(cfg["review"]["per_change"]["small"], 2)
        self.assertEqual(cfg["review"]["per_change"]["large"], 2)       # untouched default survives
        self.assertEqual(cfg["review"]["large_lines"], 99)
        self.assertEqual(cfg["limits"]["cooldown_min"], 7)
        self.assertEqual(cfg["reviewers"]["codex"]["model"], "x")
        self.assertEqual(cfg["routing"]["quota"]["primary_use"], "never")
        self.assertEqual(cfg["routing"]["quota"]["agents"]["codex"]["tier"], "reviewer")
        self.assertEqual(self.log_msgs().count("deprecated config keys"), 1)

    def test_new_keys_win_over_legacy(self):
        self.user('[review]\nmax_rounds = 3\n[limits]\nmax_rounds_per_turn = 5\n')
        self.assertEqual(config.load()["limits"]["max_rounds_per_turn"], 5)

    def test_legacy_enabled_is_not_consent(self):
        self.user('[agents.codex]\nenabled = true\n')
        cfg = config.load()
        self.assertNotIn("use", cfg["reviewers"])
        self.assertIs(cfg["reviewers"]["codex"]["enabled"], True)

    def test_repo_file_cannot_set_reviewers_models_or_mode(self):
        self.user('[reviewers]\nuse = ["codex"]\n[reviewers.claude]\nmodel = "opus"\n')
        self.repo_file('[reviewers]\nuse = ["evil"]\n[reviewers.claude]\nmodel = "x"\n[routing]\nmode = "quota"\n'
                       '[agents.codex]\nmodel = "y"\n[review]\nblock_at = "blocker"\n')
        cfg = config.load(str(self.repo))
        self.assertEqual(cfg["reviewers"]["use"], ["codex"])
        self.assertEqual(cfg["reviewers"]["claude"]["model"], "opus")
        self.assertEqual(cfg["reviewers"]["codex"]["model"], "")
        self.assertEqual(cfg["routing"]["mode"], "ordered")
        self.assertEqual(cfg["review"]["block_at"], "blocker")
        self.assertEqual(self.log_msgs().count("repo config ignored"), 1)
        dropped = next(r for r in state.read_jsonl(state.path("tagteam.log")) if r["msg"] == "repo config ignored")
        self.assertIn("reviewers.use", dropped["keys"])

    def test_repo_file_cannot_create_use_when_user_has_none(self):
        self.repo_file('[reviewers]\nuse = ["evil"]\n')
        self.assertNotIn("use", config.load(str(self.repo))["reviewers"])

    def test_sources(self):
        self.user('[triggers]\nmin_changed_lines = 9\n')
        self.repo_file('[review]\nblock_at = "minor"\n')
        cfg, src = config.load_with_sources(str(self.repo))
        self.assertEqual(src["review.block_at"], "repo")
        self.assertEqual(src["triggers.min_changed_lines"], "user")
        self.assertEqual(src["limits.cooldown_min"], "default")
        self.assertEqual(src["review.per_change.small"], "default")

    def test_string_list_shapes_are_validated(self):
        for bad in ('[reviewers]\nuse = "codex"\n', '[reviewers]\nuse = [1]\n', '[reviewers.use]\ncodex = 1\n',
                    '[authors]\nreview = "claude"\n', '[authors]\nreview = ["claude", 2]\n',
                    '[triggers]\nkinds = "code"\n', '[triggers]\nkinds = [["code"]]\n'):
            self.user(bad)
            with self.assertRaises(config.ConfigError, msg=bad):
                config.load()
        self.user('[reviewers]\nuse = ["codex"]\n[authors]\nreview = []\n[triggers]\nkinds = ["code"]\n')
        self.assertEqual(config.load()["reviewers"]["use"], ["codex"])

    def test_bad_config_raises(self):
        self.user("[[[")
        with self.assertRaises(config.ConfigError):
            config.load()
        self.user("[review]\nper_change = 3\n")
        with self.assertRaises(config.ConfigError):
            config.load()
        self.user("[reviewers.codex]\nmodel = 5\n")
        with self.assertRaises(config.ConfigError):
            config.load()
        (self.home / "config.toml").unlink()
        self.repo_file("[[[")
        with self.assertRaises(config.ConfigError):
            config.load(str(self.repo))
        self.repo_file("review = 3\n")
        with self.assertRaises(config.ConfigError):
            config.load(str(self.repo))

    def test_config_command_prints_value_and_source(self):
        self.user('[triggers]\nmin_changed_lines = 9\n')
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.assertEqual(cli.show_config(), 0)
        self.assertIn("triggers.min_changed_lines = 9  # user", out.getvalue())
        self.assertIn('review.block_at = "major"  # default', out.getvalue())


class RepoTightens(ConfigCase):
    def test_repo_cannot_loosen(self):
        self.repo_file('[limits]\nmax_rounds_per_turn = 99\n[review]\nmax_diff_chars = 9999999\nignore = ["x/*"]\n')
        cfg, src = config.load_with_sources(str(self.repo))
        self.assertEqual(cfg["limits"]["max_rounds_per_turn"], 2)
        self.assertEqual(cfg["review"]["max_diff_chars"], 60000)
        self.assertIn(".claude/settings*.json", cfg["review"]["ignore"])
        self.assertIn("x/*", cfg["review"]["ignore"])
        self.assertEqual(src["limits.max_rounds_per_turn"], "default")
        self.assertEqual(src["review.max_diff_chars"], "default")

    def test_repo_can_tighten(self):
        self.user('[limits]\nmax_rounds_per_turn = 4\n')
        self.repo_file('[limits]\nmax_rounds_per_turn = 1\n[review]\nmax_diff_chars = 100\n')
        cfg, src = config.load_with_sources(str(self.repo))
        self.assertEqual((cfg["limits"]["max_rounds_per_turn"], cfg["review"]["max_diff_chars"]), (1, 100))
        self.assertEqual(src["limits.max_rounds_per_turn"], "repo")

    def test_legacy_repo_routing_keys_log_no_false_drop(self):
        self.repo_file('[routing]\nsmall = 2\n')
        cfg = config.load(str(self.repo))
        self.assertEqual(cfg["review"]["per_change"]["small"], 2)
        self.assertNotIn("repo config ignored", self.log_msgs())


class HookFailsOpen(ConfigCase):
    def run_hook(self, event, **kw):
        repo = self.repo_dir
        payload = {"hook_event_name": event, "session_id": "s", "cwd": repo, "prompt_id": "p",
                   "transcript_path": "/x/.claude/projects/x.jsonl", **kw}
        with mock.patch("sys.stdin", new=io.StringIO(json.dumps(payload))), mock.patch("builtins.print") as pr:
            cli.hook("claude", "x")
        return json.loads(pr.call_args[0][0])

    def test_bad_user_config_fails_open_without_review(self):
        self.repo_dir = make_repo()
        self.assertEqual(self.run_hook("UserPromptSubmit", prompt="go"), {})
        pathlib.Path(self.repo_dir, "f.py").write_text("a = 1\n" * 10)
        self.user("[[[")
        with self.assertRaises(config.ConfigError):
            config.load()
        with mock.patch.object(runner, "run_all") as run_all:
            out = self.run_hook("Stop", stop_hook_active=False)
        self.assertEqual(out, {"systemMessage": "[tagteam] ⚠ Not reviewed: config error, see tagteam.log"})
        run_all.assert_not_called()
        self.assertIn("config error, not reviewing", self.log_msgs())

    def test_nested_type_mismatch_fails_open(self):
        self.repo_dir = make_repo()
        self.run_hook("UserPromptSubmit", prompt="go")
        pathlib.Path(self.repo_dir, "f.py").write_text("a = 1\n" * 10)
        self.user("[review]\nper_change = 3\n")
        with mock.patch.object(runner, "run_all") as run_all:
            out = self.run_hook("Stop", stop_hook_active=False)
        self.assertEqual(out, {"systemMessage": "[tagteam] ⚠ Not reviewed: config error, see tagteam.log"})
        run_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
