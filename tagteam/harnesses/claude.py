"""Claude Code: Claude-format hooks in ~/.claude/settings.json; `claude -p` as reviewer."""
import os
import pathlib
import sys

from .. import state
from ..adapters import HookEvent, extract_paths
from . import base

PATH = pathlib.Path.home() / ".claude" / "settings.json"
STATUSLINE_ORIG = state.HOME / "statusline.orig"
STATUSLINE_ORIG_REFRESH = state.HOME / "statusline.orig-refresh"   # user's refreshInterval before ours, or empty
REFRESH_S = 2                                                       # ticks the status line during a blocking review
TOUCH_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"


def claude_shaped_owner(p):
    """Which agent produced a Claude-format payload. Copilot also executes hooks from .claude/settings.json."""
    transcript = p.get("transcript_path") or ""
    if "/.copilot/" in transcript or isinstance(p.get("timestamp"), str):
        return "copilot"
    if "/.codex/" in transcript or ("turn_id" in p and "prompt_id" not in p):
        return "codex"
    return "claude"


def parse_claude_shaped(agent, p):
    if claude_shaped_owner(p) != agent:
        return None
    sid, cwd = p.get("session_id", ""), p.get("cwd") or os.getcwd()
    ev = HookEvent(agent, "ignore", sid, cwd, turn_id=p.get("turn_id", ""))
    name = p.get("hook_event_name")
    if name == "UserPromptSubmit":
        ev.phase, ev.prompt = "turn-start", p.get("prompt", "")
    elif name == "PostToolUse":
        ev.phase, ev.paths = "touch", extract_paths(p.get("tool_input"), cwd)
    elif name == "Stop":
        ev.phase = "turn-end"
        ev.stop_hook_active = bool(p.get("stop_hook_active"))
        ev.last_message = p.get("last_assistant_message") or ""
    elif name == "PreToolUse" and p.get("tool_name") == "ExitPlanMode":
        ev.phase = "plan-gate"
        ev.plan_text = (p.get("tool_input") or {}).get("plan", "")
    return ev


def claude_shaped_events(agent, entry_cmd, stop_timeout):
    def group(phase, timeout, matcher=None):
        g = {"hooks": [{"type": "command", "command": base.hook_cmd(entry_cmd, agent, phase), "timeout": timeout}]}
        return {"matcher": matcher, **g} if matcher else g
    events = {
        "UserPromptSubmit": group("turn-start", 30),
        "PostToolUse": group("touch", 30, TOUCH_MATCHER if agent == "claude" else None),
        "Stop": group("turn-end", stop_timeout),
    }
    if agent == "claude":
        events["PreToolUse"] = group("plan-gate", stop_timeout, "ExitPlanMode")
    return events


def install_claude_shaped(agent, path, entry_cmd, stop_timeout, dry_run, before_save=None):
    data = base.load(path)
    hooks = data.setdefault("hooks", {})
    base.strip(hooks)
    for event, entry in claude_shaped_events(agent, entry_cmd, stop_timeout).items():
        hooks.setdefault(event, []).append(entry)
    if before_save:
        before_save(data)
    base.save(path, data, dry_run)


def uninstall_claude_shaped(path, before_save=None):
    if path.exists():
        data = base.load(path)
        base.strip(data.get("hooks", {}))
        if before_save:
            before_save(data)
        base.save(path, data, False)


def wrap_statusline(data, dry_run):
    """Idempotent: re-running over an existing wrapper still upgrades its command and refreshInterval."""
    sl = data.get("statusLine") or {}
    wrapper = f"sh '{state.ROOT / 'bin' / 'tagteam-statusline'}' '{sys.executable}'"
    if not dry_run:
        STATUSLINE_ORIG.parent.mkdir(parents=True, exist_ok=True)
        if "tagteam-statusline" not in sl.get("command", ""):
            STATUSLINE_ORIG.write_text(sl.get("command", ""))
        if not STATUSLINE_ORIG_REFRESH.exists():     # first install with this field: what's there now is the user's
            STATUSLINE_ORIG_REFRESH.write_text(str(sl.get("refreshInterval", "")))
    refresh = min(sl["refreshInterval"], REFRESH_S) if "refreshInterval" in sl else REFRESH_S
    data["statusLine"] = {**sl, "type": "command", "command": wrapper, "refreshInterval": refresh}


def unwrap_statusline(data):
    if "tagteam-statusline" in (data.get("statusLine") or {}).get("command", ""):
        orig = STATUSLINE_ORIG.read_text() if STATUSLINE_ORIG.exists() else ""
        refresh = STATUSLINE_ORIG_REFRESH.read_text() if STATUSLINE_ORIG_REFRESH.exists() else ""
        if orig:
            data["statusLine"]["command"] = orig
            if refresh:
                data["statusLine"]["refreshInterval"] = int(refresh)
            else:
                data["statusLine"].pop("refreshInterval", None)
        else:
            data.pop("statusLine")
        STATUSLINE_ORIG_REFRESH.unlink(missing_ok=True)


def install(entry_cmd, stop_timeout, dry_run):
    install_claude_shaped("claude", PATH, entry_cmd, stop_timeout, dry_run,
                          before_save=lambda data: wrap_statusline(data, dry_run))


def uninstall():
    uninstall_claude_shaped(PATH, before_save=unwrap_statusline)


def argv(prompt, options, workdir, deadline):
    model = options.get("model")
    out = ["claude", "-p", "--output-format", "json", "--json-schema", base.schema_path().read_text(),
           "--permission-mode", "plan", "--disallowedTools", "Write", "Edit", "NotebookEdit"]
    return out + (["--model", model] if model else []), prompt, None


AUTHOR = base.Author(
    parse=lambda phase, payload: parse_claude_shaped("claude", payload),
    encode=lambda phase, verdict: base.encode(phase, verdict, notice=True),
    install=install, uninstall=uninstall, can_block_at_stop=True, can_notice=True, has_plan_gate=True)
REVIEWER = base.Reviewer(argv=argv, parse=lambda stdout, out_file: base.parse_wrapped(stdout, out_file, wrapped=True),
                         usage=base.no_usage)
