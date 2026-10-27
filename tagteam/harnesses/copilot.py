"""Copilot CLI: its own hook file in ~/.copilot/hooks; `copilot -p` as reviewer."""
import os
import pathlib

from .. import state
from ..adapters import HookEvent, extract_paths
from . import base

PATH = pathlib.Path.home() / ".copilot" / "hooks" / "tagteam.json"


def parse(phase, p):
    sid, cwd = p.get("sessionId", ""), p.get("cwd") or os.getcwd()
    ev = HookEvent("copilot", phase, sid, cwd)
    if phase == "turn-start":
        ev.prompt = p.get("prompt", "")
    elif phase == "touch":
        ev.paths = extract_paths(p.get("toolArgs"), cwd)
    return ev


def install(entry_cmd, stop_timeout, dry_run):
    def h(phase, t):
        return [{"type": "command", "bash": base.hook_cmd(entry_cmd, "copilot", phase), "timeoutSec": t}]
    base.save(PATH, {"version": 1, "hooks": {"userPromptSubmitted": h("turn-start", 30),
                                             "postToolUse": h("touch", 30),
                                             "agentStop": h("turn-end", stop_timeout)}}, dry_run)


def uninstall():
    if PATH.exists():
        PATH.unlink()
        print(f"removed {PATH}")


def logged_in():
    return base.signed_in_if(pathlib.Path.home() / ".copilot" / "config.json", '"lastLoggedInUser"')


def argv(prompt, options, workdir, deadline):
    model = options.get("model")
    out = ["copilot", "-p", prompt, "-s", "--no-ask-user", "--allow-all-tools", "--deny-tool", "write",
           "--deny-tool", "shell", "--usage-output-file", os.path.join(workdir, "usage.json")]
    return out + (["--model", model] if model else []), None, None


def usage(stdout, workdir):
    data = state.read_json(os.path.join(workdir, "usage.json"))
    if not isinstance(data, dict) or not data:
        return {}
    n = data.get("totalPremiumRequestCost", 1)
    return {"premium_requests": n} if isinstance(n, (int, float)) and not isinstance(n, bool) else {}


AUTHOR = base.Author(
    parse=parse, encode=lambda phase, verdict: base.encode(phase, verdict, notice=False),
    install=install, uninstall=uninstall, can_block_at_stop=True, can_notice=False, has_plan_gate=False)
REVIEWER = base.Reviewer(argv=argv, parse=lambda stdout, out_file: base.parse_wrapped(stdout, out_file), usage=usage)
