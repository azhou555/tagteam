import json
import os
import pathlib
import subprocess
import sys
import tempfile

TMP_HOME = tempfile.mkdtemp(prefix="tagteam-test-")
os.environ["TAGTEAM_HOME"] = TMP_HOME
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tagteam import state  # noqa: E402

FIX = pathlib.Path(__file__).parent / "fixtures"
CFG = state.load_config()


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


def make_repo():
    d = tempfile.mkdtemp()
    sh(d, "git", "init", "-q")
    pathlib.Path(d, "a.py").write_text("x = 1\n")
    sh(d, "git", "add", "-A")
    sh(d, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return os.path.realpath(d)


def art(path, lines=10, risk="normal", kind="code"):
    return {"path": path, "lines_changed": lines, "risk": risk, "kind": kind, "diff": "", "status": "modified"}


def finding(sev="major", conf=0.9, claim="off by one in loop", line=10, file="a.py"):
    return {"severity": sev, "file": file, "line": line, "claim": claim, "evidence": "e",
            "suggested_fix": "f", "confidence": conf}
