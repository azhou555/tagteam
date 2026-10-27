"""Codex: Claude-format hooks in ~/.codex/hooks.json; `codex exec` as reviewer."""
import json
import os
import pathlib
import re
import tomllib

from .. import state
from . import base
from .claude import install_claude_shaped, parse_claude_shaped, uninstall_claude_shaped

PATH = pathlib.Path.home() / ".codex" / "hooks.json"
CONFIG = pathlib.Path.home() / ".codex" / "config.toml"


def logged_in():
    return base.signed_in_if(pathlib.Path.home() / ".codex" / "auth.json")


def snapshot_path():
    return state.HOME / "codex-trust-at-install.json"


def _our_keys():
    hooks = json.loads(PATH.read_text()).get("hooks", {})
    return [f"{PATH}:{re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower()}:{i}:{j}"
            for event, groups in hooks.items() if isinstance(groups, list)
            for i, g in enumerate(groups) if base.ours(g)
            for j in range(len(g.get("hooks", [])))]


def _records():
    return tomllib.loads(CONFIG.read_text()).get("hooks", {}).get("state", {})


def _hash(records, key):
    h = (records.get(key) or {}).get("trusted_hash")
    return h if isinstance(h, str) else None


def save_trust_snapshot():
    """Codex keys approvals by hook position, so rewriting a hook leaves its old approval in place but stale.
    Remember which approvals existed at install so `hook_trust` can tell them from ones given after."""
    try:
        records = _records()
        snap = {k: h for k in _our_keys() if (h := _hash(records, k))}
        snapshot_path().parent.mkdir(parents=True, exist_ok=True)
        snapshot_path().write_text(json.dumps(snap, indent=2) + "\n")
    except (OSError, ValueError):
        pass


def hook_trust():
    """"unapproved" (a hook has no approval), "stale" (approval predates the last install, so Codex skips the
    hook), "trusted", or None when we can't tell. Codex's own hash is never recomputed."""
    try:
        keys, records = _our_keys(), _records()
    except (OSError, ValueError):
        return None
    if not keys:
        return None
    if any(_hash(records, k) is None for k in keys):
        return "unapproved"
    try:
        snap = json.loads(snapshot_path().read_text())
    except (OSError, ValueError):
        return None
    return "stale" if any(snap.get(k) == _hash(records, k) for k in keys) else "trusted"


def _our_entries():
    """tagteam's hook groups with their positions (Codex keys approvals by position), or None if unreadable."""
    try:
        hooks = json.loads(PATH.read_text()).get("hooks", {})
    except (OSError, ValueError):
        return None
    return [(event, i, g) for event, groups in hooks.items() if isinstance(groups, list)
            for i, g in enumerate(groups) if base.ours(g)]


def install(entry_cmd, stop_timeout, dry_run):
    before = _our_entries()
    install_claude_shaped("codex", PATH, entry_cmd, stop_timeout, dry_run)
    if not dry_run and _our_entries() != before:     # unchanged hooks keep their approvals: so must the snapshot
        save_trust_snapshot()


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
    install=install,
    uninstall=lambda: uninstall_claude_shaped(PATH),
    can_block_at_stop=True, can_notice=True, has_plan_gate=False)
REVIEWER = base.Reviewer(argv=argv, parse=lambda stdout, out_file: base.parse_wrapped(stdout, out_file),
                         usage=base.no_usage)
