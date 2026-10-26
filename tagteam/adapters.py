"""Translate each agent's hook payload into a HookEvent, and a Verdict back into its stdout.

Per-agent parsing and encoding live in tagteam/harnesses. Verified against captured payloads in tests/fixtures (2026-10-25). Copilot also executes
Claude-format hooks from .claude/settings.json, so the claude adapter rejects payloads that
came from another agent; that agent's own adapter handles the event instead.
"""
import dataclasses
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


def parse(agent, phase, payload):
    from . import harnesses
    return harnesses.get(agent).author.parse(phase, payload)


def encode(agent, phase, verdict):
    from . import harnesses
    return harnesses.get(agent).author.encode(phase, verdict)
