"""Author and Reviewer roles, plus the pieces every harness shares: hook-config helpers and output parsing."""
import dataclasses
import json
import os
import re
import shutil
import time
from typing import Callable

from .. import resources

TAG = "tagteam-hook-form=1"
SEVERITIES = ("blocker", "major", "minor", "nit")


@dataclasses.dataclass(frozen=True)
class Author:
    parse: Callable            # (phase, payload) -> HookEvent | None
    encode: Callable           # (phase, verdict) -> stdout
    install: Callable          # (entry_cmd, stop_timeout, dry_run)
    uninstall: Callable
    can_block_at_stop: bool
    can_notice: bool
    has_plan_gate: bool
    can_add_context: bool      # turn-start output reaches the model (UserPromptSubmit additionalContext)


@dataclasses.dataclass(frozen=True)
class Reviewer:
    argv: Callable             # (prompt, options, workdir, deadline) -> (argv, stdin_text, output_file)
    parse: Callable            # (stdout, out_file) -> normalized result | None
    usage: Callable            # (stdout, workdir) -> subset of {input_tokens, output_tokens, cost_usd, premium_requests}
    reads_repo: bool = True


def no_usage(stdout, workdir):
    return {}


def envelope_usage(stdout, cost_key=None):
    """Best-effort usage from a CLI's JSON envelope: {input_tokens, output_tokens, cost_usd}; {} on any surprise."""
    env = extract_json(stdout)
    if not isinstance(env, dict):
        return {}
    u = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    found = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
             "cost_usd": env.get(cost_key) if cost_key else None}
    return {k: v for k, v in found.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def signed_in_if(path, needle=None):
    """True if the CLI's local credential file exists (and mentions `needle`); None when we can't tell offline."""
    try:
        text = path.read_text() if needle else ""
        return True if path.exists() and (not needle or needle in text) else None
    except OSError:
        return None


def schema_path():
    return resources.path("findings.schema.json")


def encode(phase, verdict, notice, block_decision="block", context=False):
    """verdict: None (no-op) or {"action": "block" | "allow" | "context", "message": str}.

    An allow message is a notice for the user; only harnesses with `notice` have a field to show it. A context
    message is for the model at turn start; only harnesses with `context` take it.
    """
    if not verdict:
        return "{}"
    msg = verdict["message"]
    if verdict["action"] == "context":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                  "additionalContext": msg}}) if context else "{}"
    if verdict["action"] != "block":
        return json.dumps({"systemMessage": msg}) if msg and notice else "{}"
    if phase == "plan-gate":
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                  "permissionDecision": "deny",
                                                  "permissionDecisionReason": msg}})
    return json.dumps({"decision": block_decision, "reason": msg})


def hook_cmd(entry_cmd, agent, phase):
    return f": {TAG}; {entry_cmd} hook {agent} {phase}"


def ours(entry):
    return TAG in json.dumps(entry)


def load(p):
    return json.loads(p.read_text()) if p.exists() else {}


def save(p, data, dry_run):
    text = json.dumps(data, indent=2) + "\n"
    if dry_run:
        print(f"--- {p} (dry run)\n{text}")
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        shutil.copyfile(p, p.with_name(p.name + f".tagteam-bak-{time.strftime('%Y%m%d%H%M%S')}"))
    tmp = p.with_name(p.name + ".tagteam-tmp")
    tmp.write_text(text)
    json.loads(tmp.read_text())                     # re-validate before replacing
    tmp.replace(p)
    print(f"updated {p}")


def strip(hooks):
    for event in list(hooks):
        if isinstance(hooks[event], list):
            hooks[event] = [e for e in hooks[event] if not ours(e)]
            if not hooks[event]:
                del hooks[event]


def extract_json(text):
    text = (text or "").strip()
    for candidate in (text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text)):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            pass
    return None


def normalize(data):
    if not isinstance(data, dict) or "findings" not in data:
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not f.get("claim"):
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
        try:
            conf = float(f.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        findings.append({"severity": sev, "file": str(f.get("file") or ""), "line": f.get("line"),
                         "claim": str(f["claim"]), "evidence": str(f.get("evidence") or ""),
                         "suggested_fix": str(f.get("suggested_fix") or ""), "confidence": conf})
    verdict = data.get("verdict") if data.get("verdict") in ("approve", "changes_requested") else (
        "changes_requested" if findings else "approve")
    return {"verdict": verdict, "summary": str(data.get("summary") or ""), "findings": findings}


def parse_wrapped(stdout, out_file=None, wrapped=False):
    """Findings from stdout (or out_file); `wrapped` for CLIs whose JSON envelope carries them."""
    if out_file and os.path.exists(out_file):
        data = extract_json(open(out_file).read())
    else:
        data = extract_json(stdout)
    if isinstance(data, dict) and wrapped:
        so = data.get("structured_output")
        data = so if isinstance(so, dict) else extract_json(data.get("result") or data.get("response") or "")
    return normalize(data)
