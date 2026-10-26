"""Translate each agent's hook payload into a HookEvent, and a Verdict back into its stdout.

Verified against captured payloads in tests/fixtures (2026-10-25). Copilot also executes
Claude-format hooks from .claude/settings.json, so the claude adapter rejects payloads that
came from another agent; that agent's own adapter handles the event instead.
"""
import dataclasses
import json
import os
import re

MARKER = "[tagteam]"
PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
PATH_KEYS = {"file_path", "filePath", "path", "Path", "TargetFile", "notebook_path", "AbsolutePath"}


@dataclasses.dataclass
class HookEvent:
    agent: str
    phase: str                      # turn-start | touch | turn-end | plan-gate | ignore
    session_id: str
    cwd: str
    prompt: str = ""
    paths: list = dataclasses.field(default_factory=list)
    stop_hook_active: bool = False
    turn_id: str = ""
    last_message: str = ""
    plan_text: str = ""


def extract_paths(tool_input, cwd):
    """Best-effort file paths from any agent's tool input (dict args or apply_patch text)."""
    found = []

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k in PATH_KEYS and isinstance(x, str):
                    found.append(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            found.extend(m.strip() for m in PATCH_FILE.findall(v))

    walk(tool_input)
    return sorted({os.path.normpath(os.path.join(cwd, p)) for p in found})


def _claude_shaped_owner(p):
    """Which agent produced a Claude-format payload."""
    transcript = p.get("transcript_path") or ""
    if "/.copilot/" in transcript or isinstance(p.get("timestamp"), str):
        return "copilot"
    if "/.codex/" in transcript or ("turn_id" in p and "prompt_id" not in p):
        return "codex"
    return "claude"


def parse_claude_shaped(agent, p):
    if _claude_shaped_owner(p) != agent:
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


def parse_copilot(p, phase):
    sid, cwd = p.get("sessionId", ""), p.get("cwd") or os.getcwd()
    ev = HookEvent("copilot", phase, sid, cwd)
    if phase == "turn-start":
        ev.prompt = p.get("prompt", "")
    elif phase == "touch":
        ev.paths = extract_paths(p.get("toolArgs"), cwd)
    elif phase == "turn-end":
        ev.stop_hook_active = bool(p.get("stop_hook_active"))
    return ev


def parse_agy(p, phase):
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
    elif phase == "turn-end":
        ev.stop_hook_active = p.get("executionNum", 0) > 0
    return ev


def parse(agent, phase, payload):
    if agent in ("claude", "codex"):
        return parse_claude_shaped(agent, payload)
    if agent == "copilot":
        return parse_copilot(payload, phase)
    if agent == "agy":
        return parse_agy(payload, phase)
    raise ValueError(f"unknown agent {agent}")


def encode(agent, phase, verdict):
    """verdict: None (allow / no-op) or {"action": "block", "message": str}."""
    if not verdict or verdict.get("action") != "block":
        return "{}"
    msg = verdict["message"]
    if phase == "plan-gate":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": "deny",
                                                  "permissionDecisionReason": msg}})
    if agent == "agy":
        return json.dumps({"decision": "continue", "reason": msg})
    return json.dumps({"decision": "block", "reason": msg})
