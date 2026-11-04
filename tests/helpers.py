import io
import json
import os
import pathlib
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMP_HOME = tempfile.mkdtemp(prefix="tagteam-test-")
os.environ["TAGTEAM_HOME"] = TMP_HOME
os.environ.pop("TAGTEAM_NO_MACOS_EXTRAS", None)       # clean-install runs set it; tests that need it set it themselves
sys.path.insert(0, str(ROOT))

from tagteam import config, core, state  # noqa: E402,F401

REAL_DETACH = core._detach


def _no_detach(argv, fd):
    raise AssertionError(f"a test started a real background review: {argv}")


core._detach = _no_detach                            # a detached child has no stubs: it would run real reviewers

_run = unittest.TestCase.run


def _clean_run(self, result=None):
    """Every test starts with an empty state dir and no user config, so reservations, failure counts and
    cooldowns never leak between tests or modules."""
    shutil.rmtree(pathlib.Path(TMP_HOME) / "state", ignore_errors=True)
    (pathlib.Path(TMP_HOME) / "config.toml").unlink(missing_ok=True)
    config._legacy_noted = False
    return _run(self, result)


unittest.TestCase.run = _clean_run

FIX = pathlib.Path(__file__).parent / "fixtures"
CFG = config.load()


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_repo():
    d = tempfile.mkdtemp()
    sh(d, "git", "init", "-q")
    sh(d, "git", "config", "user.email", "t@t")          # later commits in the repo need it too (CI has no identity)
    sh(d, "git", "config", "user.name", "t")
    pathlib.Path(d, "a.py").write_text("x = 1\n")
    sh(d, "git", "add", "-A")
    sh(d, "git", "commit", "-qm", "init")
    return os.path.realpath(d)


def art(path, lines=10, risk="normal", kind="code"):
    return {"path": path, "lines_changed": lines, "risk": risk, "kind": kind, "diff": "", "status": "modified"}


def finding(sev="major", conf=0.9, claim="off by one in loop", line=10, file="a.py"):
    return {"severity": sev, "file": file, "line": line, "claim": claim, "evidence": "e",
            "suggested_fix": "f", "confidence": conf}


FULL = {"claude": 0.9, "codex": 0.9, "copilot": 0.9, "agy": 0.9}
ALL_AGENTS = ["claude", "codex", "copilot", "agy"]
USER_CONFIG = state.HOME / "config.toml"
PIN_TEXT = "review.blocking.small = true\n"    # HookRig tests drive the blocking cycle unless they opt out
PIN = [""]


def write_user_config(text):
    USER_CONFIG.write_text(PIN[0] + text)


def clear_user_config():
    USER_CONFIG.unlink(missing_ok=True)


def use_toml(use):
    return "[reviewers]\nuse = [" + ", ".join(f'"{a}"' for a in use) + "]\n"


class HookRig:
    """Mixin: real hook flow through cli.hook with every agent detected and reviewer CLIs stubbed."""

    def rig(self, user_config=None, small_blocks=True):
        from tagteam import core, harnesses
        PIN[0] = PIN_TEXT if small_blocks else ""
        self.repo = make_repo()
        self.calls = []
        self.seen_active = []
        self.detached = []

        def detach(argv, fd):                        # a background review, run here so the stubs apply
            self.detached.append(argv[-1])
            if self.run_detached:
                core.background(argv[-1])
        self.run_detached = True
        self.rigs = [mock.patch.object(core, "headroom_map", return_value=FULL),
                     mock.patch.object(harnesses, "detected", return_value=list(ALL_AGENTS)),
                     mock.patch.object(core, "_detach", detach)]
        for x in self.rigs:
            x.start()
        clear_user_config()
        if user_config is not None:
            write_user_config(user_config)

    def unrig(self):
        for x in self.rigs:
            x.stop()
        clear_user_config()
        PIN[0] = ""

    def run_hook(self, agent, phase, payload):
        from tagteam import cli
        with mock.patch("sys.stdin", new=io.StringIO(json.dumps(payload))), mock.patch("builtins.print") as pr:
            cli.hook(agent, phase)
        return json.loads(pr.call_args[0][0])

    def fake_run_all(self, results):
        from tagteam import live

        def run_all(jobs, cfg, cwd, run_dir, deadline, on_done=None):
            self.calls.append(jobs)
            self.seen_active.append(live.active())
            out = {r: ({"reviewer": r, "ok": True, "duration": 1}, results.get(r, {"verdict": "approve", "summary": "",
                                                                                 "findings": []})) for r in jobs}
            for r, (meta, _) in out.items():
                on_done(r, meta)
            return out
        return run_all

    def claude(self, event, **kw):
        return {"hook_event_name": event, "session_id": "sess1", "cwd": self.repo, "prompt_id": "p",
                "transcript_path": "/Users/u/.claude/projects/x.jsonl", **kw}
