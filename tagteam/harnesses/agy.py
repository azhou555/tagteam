"""agy (Antigravity CLI): hooks under a `tagteam` key in ~/.gemini/config/hooks.json; `agy -p` as reviewer."""
import json
import os
import pathlib

from ..adapters import HookEvent, extract_paths
from . import base

PATH = pathlib.Path.home() / ".gemini" / "config" / "hooks.json"
# agy headless auto-denies shell commands and then returns an empty response, so steer it off them.
NOTE = ("\nIMPORTANT: shell/terminal commands are disabled in this session and any attempt aborts the review. "
        "Do not run commands (no git, cat, ls, grep). The diff above has what you need; use only file-viewing "
        "tools if you must read more.")


def parse(phase, p):
    ws = p.get("workspacePaths") or [os.getcwd()]
    ev = HookEvent("agy", phase, p.get("conversationId", ""), ws[0])
    if phase == "turn-start":
        # PreInvocation fires before every model call; invocationNum 0 is a new user turn.
        if p.get("invocationNum", 0) != 0:
            ev.phase = "ignore"
        raw = p.get("lastUserInput", "")
        try:
            ev.prompt = json.loads(raw) if raw.startswith('"') else raw
        except ValueError:
            ev.prompt = raw
    elif phase == "touch":
        ev.paths = extract_paths((p.get("toolCall") or {}).get("args"), ws[0])
    return ev


def install(entry_cmd, stop_timeout, dry_run):
    data = base.load(PATH)
    data["tagteam"] = {
        "PreInvocation": [{"type": "command", "command": base.hook_cmd(entry_cmd, "agy", "turn-start"), "timeout": 30}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command",
                                                    "command": base.hook_cmd(entry_cmd, "agy", "touch"),
                                                    "timeout": 30}]}],
        "Stop": [{"type": "command", "command": base.hook_cmd(entry_cmd, "agy", "turn-end"),
                  "timeout": stop_timeout}],
    }
    base.save(PATH, data, dry_run)


def uninstall():
    if PATH.exists():
        data = base.load(PATH)
        if data.pop("tagteam", None) is not None:
            base.save(PATH, data, False)


def logged_in():
    return base.signed_in_if(pathlib.Path.home() / ".gemini" / "oauth_creds.json")


def argv(prompt, options, workdir, deadline):
    model, effort = options.get("model"), options.get("effort")
    out = ["agy", "-p", prompt + NOTE, "--mode", "plan", "--output-format", "json", "--json-schema",
           str(base.schema_path()), "--print-timeout", f"{deadline}s"]
    return out + (["--model", model] if model else []) + (["--effort", effort] if effort else []), None, None


AUTHOR = base.Author(
    parse=parse, encode=lambda phase, verdict: base.encode(phase, verdict, notice=False, block_decision="continue"),
    install=install, uninstall=uninstall, can_block_at_stop=True, can_notice=False, has_plan_gate=False,
    can_add_context=False)      # PreInvocation output is undocumented (Antigravity CLI docs, 2026-10-27)
REVIEWER = base.Reviewer(argv=argv, parse=lambda stdout, out_file: base.parse_wrapped(stdout, out_file, wrapped=True),
                         usage=lambda stdout, workdir: base.envelope_usage(stdout))
