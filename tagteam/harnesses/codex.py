"""Codex: Claude-format hooks in ~/.codex/hooks.json; `codex exec` as reviewer."""
import json
import os
import pathlib
import re
import tomllib

from . import base
from .claude import install_claude_shaped, parse_claude_shaped, uninstall_claude_shaped

PATH = pathlib.Path.home() / ".codex" / "hooks.json"
CONFIG = pathlib.Path.home() / ".codex" / "config.toml"


def logged_in():
    return base.signed_in_if(pathlib.Path.home() / ".codex" / "auth.json")


def hook_trust():
    """True if every tagteam hook has a trusted_hash record in config.toml, False if any lacks one, None if unreadable.
    Presence only: the hash itself (canonical TOML sha256) is not recomputed, so an edited hook still reads as trusted."""
    try:
        hooks = json.loads(PATH.read_text()).get("hooks", {})
        trusted = tomllib.loads(CONFIG.read_text()).get("hooks", {}).get("state", {})
    except (OSError, ValueError):
        return None
    keys = [f"{PATH}:{re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower()}:{i}:{j}"
            for event, groups in hooks.items() if isinstance(groups, list)
            for i, g in enumerate(groups) if base.ours(g)
            for j in range(len(g.get("hooks", [])))]
    return all("trusted_hash" in (trusted.get(k) or {}) for k in keys) if keys else None


def argv(prompt, options, workdir, deadline):
    model = options.get("model")
    out = os.path.join(workdir, "out.json")
    # approval_policy=never matters: with the user's auto_review approver, read-only alone let apply_patch through.
    cmd = ["codex", "exec", "-s", "read-only", "-c", 'approval_policy="never"', "--skip-git-repo-check",
           "--output-schema", str(base.schema_path()), "-o", out]
    return cmd + (["-m", model] if model else []) + ["-"], prompt, out


AUTHOR = base.Author(
    parse=lambda phase, payload: parse_claude_shaped("codex", payload),
    encode=lambda phase, verdict: base.encode(phase, verdict, notice=True),
    install=lambda entry_cmd, stop_timeout, dry_run: install_claude_shaped("codex", PATH, entry_cmd, stop_timeout,
                                                                           dry_run),
    uninstall=lambda: uninstall_claude_shaped(PATH),
    can_block_at_stop=True, can_notice=True, has_plan_gate=False)
REVIEWER = base.Reviewer(argv=argv, parse=lambda stdout, out_file: base.parse_wrapped(stdout, out_file),
                         usage=base.no_usage)
