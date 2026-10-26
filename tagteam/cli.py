"""tagteam CLI: `hook <agent> <phase>`, `status`, `install`, `uninstall`."""
import argparse
import json
import os
import sys
import time
import traceback

from . import adapters, core, install, quota, state


def hook(agent, phase):
    """Entry point for every agent hook. Never raises and never blocks on its own failure."""
    raw = sys.stdin.read()
    if os.environ.get("TAGTEAM_DEPTH") or os.environ.get("TAGTEAM", "").lower() == "off":
        print("{}")
        return
    ev = None
    try:
        ev = adapters.parse(agent, phase, json.loads(raw or "{}"))
        if ev is None or ev.phase == "ignore":
            print("{}")
            return
        repo = core.changes.repo_root(ev.cwd)
        if repo and os.path.exists(os.path.join(repo, ".tagteam-off")):
            print("{}")
            return
        cfg = state.load_config(repo)
        verdict = core.HANDLERS[ev.phase](ev, cfg)
        print(adapters.encode(agent, ev.phase, verdict))
    except Exception as e:
        state.log("hook error", agent=agent, phase=phase, error=repr(e), tb=traceback.format_exc()[-2000:])
        print("{}")


def status(days):
    cfg = state.load_config()
    now = time.time()
    print("Headroom (fraction of usage limit left):")
    for a in cfg["agents"]:
        h = quota.headroom(a, cfg, now)
        tier = cfg["agents"][a]["tier"]
        print(f"  {a:8} {tier:8} {'unknown' if h is None else f'{h:.0%}'}")
    hist = state.read_jsonl(state.path("history.jsonl"), now - days * 86400)
    print(f"\nReviews in the last {days}d: {len(hist)}")
    by = {}
    for r in hist:
        b = by.setdefault(r["reviewer"], {"n": 0, "fail": 0, "secs": 0.0})
        b["n"] += 1
        b["fail"] += 0 if r.get("ok") else 1
        b["secs"] += r.get("duration", 0)
    for a, b in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        share = b["n"] / len(hist)
        print(f"  {a:8} {b['n']:4} ({share:.0%})  failed {b['fail']}  avg {b['secs'] / b['n']:.0f}s")
    runs = sorted(state.path("runs", "x").parent.glob("*/verdict.json"))[-5:]
    if runs:
        print("\nRecent runs:")
        for v in runs:
            d = state.read_json(v, {})
            req = state.read_json(v.parent / "request.json", {})
            print(f"  {v.parent.name}  {req.get('author', '?'):7} -> {','.join(req.get('assignments', {})):20} "
                  f"{d.get('action')}  blocking={len(d.get('blocking', []))}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tagteam")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("agent", choices=["claude", "codex", "copilot", "agy"])
    h.add_argument("phase")
    s = sub.add_parser("status")
    s.add_argument("--days", type=int, default=7)
    i = sub.add_parser("install")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--agents", default="claude,codex,copilot,agy")
    sub.add_parser("uninstall")
    a = p.parse_args(argv)
    if a.cmd == "hook":
        hook(a.agent, a.phase)
    elif a.cmd == "status":
        status(a.days)
    elif a.cmd == "install":
        install.install(a.agents.split(","), dry_run=a.dry_run)
    elif a.cmd == "uninstall":
        install.uninstall()
