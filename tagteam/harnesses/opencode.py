"""OpenCode: a JS plugin in ~/.config/opencode/plugins that pipes each hook to tagteam; `opencode run` as reviewer.

The plugin serves OpenCode 1.x (its server() hooks) and 2.x (its setup()). The reviewer argv is 2.x only.

OpenCode has no blocking Stop hook. The plugin runs the turn-end review when a session goes idle and, on a block,
sends the findings back as the next prompt. In 1.x, headless `opencode run` exits at idle, so the plugin is inert there.
"""
import json
import os
import pathlib

from .. import resources
from ..adapters import HookEvent, extract_paths
from . import base

PATH = pathlib.Path.home() / ".config" / "opencode" / "plugins" / "tagteam.js"
# Everything is denied but reading, so a tool OpenCode adds later is denied too.
READ_ONLY = [{"action": "*", "resource": "*", "effect": "deny"}] + [
    {"action": action, "resource": "*", "effect": "allow"} for action in ("read", "glob", "grep")]
# A repo's own opencode.json could allow edits, so project config is off. The reviewer runs as an agent only this
# config defines, so no user or extra config file can loosen its permissions. No snapshots: each review would
# otherwise add ~20 files to OpenCode's undo store for the user's project.
REVIEW_CONFIG = json.dumps({"snapshot": False, "permissions": READ_ONLY, "agents": {"tagteam-review": {
    "mode": "primary", "description": "tagteam's read-only reviewer", "permissions": READ_ONLY}}})


def parse(phase, p):
    cwd = p.get("cwd") or os.getcwd()
    ev = HookEvent("opencode", phase, p.get("sessionID", ""), cwd)
    if phase == "turn-start":
        raw = p.get("prompt", "")
        try:
            ev.prompt = json.loads(raw) if raw.startswith('"') else raw    # `opencode run` quotes its message
        except ValueError:
            ev.prompt = raw
    elif phase == "touch":
        ev.paths = extract_paths(p.get("args"), cwd)
    return ev


def render(entry_cmd, stop_timeout):
    hooks = {phase: base.hook_cmd(entry_cmd, "opencode", phase) for phase in ("turn-start", "touch", "turn-end")}
    timeouts = {"turn-start": 30, "touch": 30, "turn-end": stop_timeout}
    return (resources.text("opencode-plugin.js").replace("__HOOKS__", json.dumps(hooks))
            .replace("__TIMEOUT_S__", json.dumps(timeouts)))


def install(entry_cmd, stop_timeout, dry_run):
    return base.write(PATH, render(entry_cmd, stop_timeout), dry_run)


def uninstall():
    if PATH.exists() and base.TAG in PATH.read_text(errors="ignore"):     # the folder holds the user's plugins too
        PATH.unlink()
        print(f"removed {PATH}")


def logged_in():
    return base.signed_in_if(pathlib.Path.home() / ".local" / "share" / "opencode" / "auth.json")


def argv(prompt, options, workdir, deadline):
    model, effort = options.get("model"), options.get("effort")
    if model and effort:
        model += f"#{effort}"
    # OpenCode takes its directory from $PWD, which still names the author's repo, not the review's checkout.
    # --standalone: a private server that reads this env, not the user's shared background service.
    cmd = ["env", "-u", "PWD", "OPENCODE_DISABLE_PROJECT_CONFIG=1", f"OPENCODE_CONFIG_CONTENT={REVIEW_CONFIG}",
           "opencode", "run", "--standalone", "--agent", "tagteam-review", "--title", "tagteam review",
           "--format", "json"]
    return cmd + (["-m", model] if model else []) + ["--", prompt], None, None


def _events(stdout, kind):
    for line in (stdout or "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") == kind and isinstance(e.get("part"), dict):
            yield e["part"]


def parse_result(stdout, out_file):
    """Findings from the last text part, or from all of them joined when the answer spans parts."""
    texts = [p.get("text") or "" for p in _events(stdout, "text")]
    for text in (texts[-1:] + ["\n".join(texts)]) if texts else ():
        result = base.normalize(base.extract_json(text))
        if result is not None:
            return result
    return None


def usage(stdout, workdir):
    steps = list(_events(stdout, "step_finish"))
    if not steps:
        return {}
    total = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for s in steps:
        tokens = s.get("tokens") if isinstance(s.get("tokens"), dict) else {}
        for key, field in (("input_tokens", "input"), ("output_tokens", "output")):
            if isinstance(tokens.get(field), int):
                total[key] += tokens[field]
        if isinstance(s.get("cost"), (int, float)):
            total["cost_usd"] += s["cost"]
    return total


AUTHOR = base.Author(
    parse=parse, encode=lambda phase, verdict: base.encode(phase, verdict, notice=True, context=True),
    install=install, uninstall=uninstall,
    can_block_at_stop=True,     # in the TUI: the plugin sends the findings as the next prompt (live, 1.19.5 and 2.2.0)
    can_notice=True,            # the plugin shows systemMessage as a TUI toast (1.x only: 2.x has no toast for a plugin)
    has_plan_gate=False,
    can_add_context=True)       # the plugin adds additionalContext as a synthetic message (live, 1.19.5 and 2.2.0)
REVIEWER = base.Reviewer(argv=argv, parse=parse_result, usage=usage)
