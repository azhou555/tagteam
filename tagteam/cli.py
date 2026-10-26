"""tagteam CLI: `hook <agent> <phase> [--check]`, `statusline`, `swiftbar`, `config`, `status`, `watch`, `install`, `uninstall`, `doctor`."""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback

from . import adapters, config, core, doctor, harnesses, install, live, quota, state


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
        try:
            cfg = config.load(repo)
        except config.ConfigError as e:
            state.log("config error, not reviewing", agent=agent, phase=phase, error=str(e))
            notice = {"action": "allow", "message": f"{adapters.MARKER} ⚠ Not reviewed: config error, see tagteam.log"}
            print(adapters.encode(agent, ev.phase, notice if ev.phase in ("turn-end", "plan-gate") else None))
            return
        verdict = core.HANDLERS[ev.phase](ev, cfg)
        print(adapters.encode(agent, ev.phase, verdict))
    except Exception as e:
        state.log("hook error", agent=agent, phase=phase, error=repr(e), tb=traceback.format_exc()[-2000:])
        try:
            live.note_error()
        except Exception:
            pass
        print("{}")


def check(agent, phase):
    """Fails closed, unlike `hook`: parse stdin with the real harness parser and run nothing else."""
    try:
        ev = adapters.parse(agent, phase, json.loads(sys.stdin.read()))
        if ev is None or ev.phase == "ignore":
            raise ValueError(f"payload is not a {agent} {phase} event")
    except Exception as e:
        print(f"{agent} {phase}: {e!r}", file=sys.stderr)
        return 1
    print(f"ok {agent} {phase}")
    return 0


def statusline():
    """Claude Code status line: save its JSON for the quota reader, show the user's original line, then ours."""
    try:
        raw = sys.stdin.buffer.read().decode(errors="replace")
    except Exception:
        raw = ""
    try:
        q = state.path("quota", "claude.json")
        tmp = q.with_name(f"claude.json.{os.getpid()}")
        tmp.write_text(raw)
        tmp.replace(q)
    except Exception:
        pass
    try:
        orig = state.HOME / "statusline.orig"
        if orig.exists() and orig.read_text().strip():
            r = subprocess.run(["sh", "-c", orig.read_text()], input=raw, capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.rstrip("\n"):
                print(r.stdout.rstrip("\n"))
    except Exception:
        pass
    try:
        print(live.statusline_segment(raw))
    except Exception:
        pass


def status(days):
    cfg = config.load()
    now = time.time()
    print("Headroom (fraction of usage limit left):")
    for a, ac in cfg["routing"]["quota"]["agents"].items():
        h = quota.headroom(a, cfg, now)
        tier = ac["tier"]
        print(f"  {a:8} {tier:8} {'unknown' if h is None else f'{h:.0%}'}")
    runs = state.read_jsonl(state.path("runs.jsonl"), now - days * 86400)
    print(f"\nReviews in the last {days}d: {len(runs)}  (blocked {sum(r['action'] == 'block' for r in runs)})")
    hist = state.read_jsonl(state.path("history.jsonl"), now - days * 86400)
    print(f"Reviewer calls in the last {days}d: {len(hist)}")
    by = {}
    for r in hist:
        b = by.setdefault(r["reviewer"], {"n": 0, "fail": 0, "secs": 0.0})
        b["n"] += 1
        b["fail"] += 0 if r.get("ok") else 1
        b["secs"] += r.get("duration", 0)
    for a, b in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        share = b["n"] / len(hist)
        print(f"  {a:8} {b['n']:4} ({share:.0%})  failed {b['fail']}  avg {b['secs'] / b['n']:.0f}s")
    print("\n" + "\n".join(live.week_lines(now)))
    running = live.active(now)
    if running:
        print("\nRunning now:")
        for r in running:
            print(f"  {r['run_id']}  {r['author']:7} -> {','.join(r['reviewers'])}  {now - r['started']:.0f}s")
    if runs:
        print("\nRecent runs:")
        for r in runs[-5:]:
            print(f"  {r['run_id']}  {r['author']:7} -> {','.join(r['reviewers']) or '-':20} {live.summary(r)}")


def show_config():
    try:
        cfg, sources = config.load_with_sources(core.changes.repo_root(os.getcwd()))
    except config.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    for k, v in config.flat(cfg).items():
        print(f"{k} = {json.dumps(v)}  # {sources[k]}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tagteam")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook")
    h.add_argument("agent", choices=["claude", "codex", "copilot", "agy"])
    h.add_argument("phase")
    h.add_argument("--check", action="store_true", help="parse stdin only; exit 1 if it is not a valid payload")
    sub.add_parser("statusline")
    sub.add_parser("swiftbar")
    sub.add_parser("config", help="print the effective config and where each value came from")
    s = sub.add_parser("status")
    s.add_argument("--days", type=int, default=7)
    i = sub.add_parser("install")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--agents", default="claude,codex,copilot,agy")
    i.add_argument("--reviewers", help="agents that may review your changes, in order; saves reviewers.use")
    i.add_argument("--authors", help="agents whose turns get reviewed; saves authors.review")
    i.add_argument("--yes", action="store_true", help="don't prompt; without --reviewers no reviewer list is saved")
    d = sub.add_parser("doctor", help="check each agent's hooks, login and reviewer status; read-only")
    d.add_argument("--probe", action="store_true", help="also run one tiny real review per listed reviewer (costs money)")
    d.add_argument("--clear", action="store_true", help="re-enable reviewers the sandbox disabled, then check")
    w = sub.add_parser("watch")
    w.add_argument("--once", action="store_true")
    sub.add_parser("uninstall")
    a = p.parse_args(argv)
    if a.cmd == "hook":
        if a.check:
            sys.exit(check(a.agent, a.phase))
        hook(a.agent, a.phase)
    elif a.cmd == "statusline":
        statusline()
    elif a.cmd == "swiftbar":
        live.main(["swiftbar"])
    elif a.cmd == "config":
        sys.exit(show_config())
    elif a.cmd == "status":
        status(a.days)
    elif a.cmd == "doctor":
        sys.exit(doctor.run(a.probe, a.clear))
    elif a.cmd == "watch":
        live.watch(once=a.once)
    elif a.cmd == "install":
        names = {k: a.__dict__[k].split(",") if a.__dict__[k] else None for k in ("reviewers", "authors")}
        bad = [n for v in names.values() for n in v or [] if n not in harnesses.ALL]
        if bad:
            p.error(f"unknown agent: {', '.join(bad)} (choose from {', '.join(harnesses.ALL)})")
        try:
            install.install(a.agents.split(","), dry_run=a.dry_run)
            install.set_consent(names["reviewers"], names["authors"], a.yes, a.dry_run)
        except config.ConfigError as e:
            sys.exit(f"config error: {e}")
    elif a.cmd == "uninstall":
        install.uninstall()
