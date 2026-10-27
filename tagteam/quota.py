"""Headroom (fraction of usage limit remaining, 0..1) per agent. None = unknown.

Sources verified 2026-10-25:
  claude  - status line stdin `rate_limits.{five_hour,seven_day}.used_percentage`, cached by `tagteam statusline`
  codex   - `rate_limits.primary/secondary.used_percent` lines in ~/.codex/sessions rollout JSONL
  copilot - `gh api /copilot_internal/user` premium_interactions.percent_remaining (undocumented; self-meter fallback)
  agy     - nothing exposed; self-metered runs per day
"""
import glob
import json
import os
import pathlib
import subprocess
import time

from . import state


def _pct_window(used, resets_at, now):
    if used is None:
        return None
    if resets_at and resets_at < now:
        return 1.0
    return max(0.0, 1.0 - used / 100.0)


def claude(now):
    p = state.path("quota", "claude.json")
    data = state.read_json(p)
    if not data or now - p.stat().st_mtime > 5 * 3600:
        return None
    rl = data.get("rate_limits") or {}
    vals = [_pct_window((rl.get(w) or {}).get("used_percentage"), (rl.get(w) or {}).get("resets_at"), now)
            for w in ("five_hour", "seven_day")]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def codex(now, home=None):
    home = home or os.environ.get("CODEX_HOME", str(pathlib.Path.home() / ".codex"))
    files = sorted(glob.glob(f"{home}/sessions/*/*/*/rollout-*.jsonl"), key=os.path.getmtime, reverse=True)[:8]
    best = None                                     # (event timestamp, headroom)
    for f in files:
        if now - os.path.getmtime(f) > 7 * 86400:
            break
        with open(f, "rb") as fh:
            fh.seek(max(0, os.path.getsize(f) - 512_000))
            tail = fh.read().decode(errors="ignore")
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rl = _find(rec, "rate_limits")
            if not rl:
                continue
            vals = [_pct_window((rl.get(w) or {}).get("used_percent"), (rl.get(w) or {}).get("resets_at"), now)
                    for w in ("primary", "secondary")]
            vals = [v for v in vals if v is not None]
            if vals and (best is None or rec.get("timestamp", "") > best[0]):
                best = (rec.get("timestamp", ""), min(vals))
            break
    return best[1] if best else None


def _find(obj, key):
    if isinstance(obj, dict):
        if isinstance(obj.get(key), dict):
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r:
                return r
    return None


def copilot(now, cfg):
    cache = state.path("quota", "copilot.json")
    data = state.read_json(cache)
    if not data or now - data.get("ts", 0) > 600:
        try:
            r = subprocess.run(["gh", "api", "/copilot_internal/user"], capture_output=True, text=True, timeout=10)
            snap = json.loads(r.stdout)["quota_snapshots"]["premium_interactions"]
            data = {"ts": now, "headroom": 1.0 if snap.get("unlimited") else snap["percent_remaining"] / 100.0}
            state.write_json(cache, data)
        except Exception as e:                       # undocumented endpoint: fall back to self-metering
            state.log("copilot quota api failed", error=str(e))
            spent = sum(r.get("cost", 1) for r in state.read_jsonl(state.path("history.jsonl"), now - 30 * 86400)
                        if r.get("reviewer") == "copilot")
            return max(0.0, 1.0 - spent / cfg["quota"].get("monthly_budget", 200))
    return data["headroom"]


def self_metered(agent, now, cfg):
    runs = [r for r in state.read_jsonl(state.path("history.jsonl"), now - 86400) if r.get("reviewer") == agent]
    return max(0.0, 1.0 - len(runs) / cfg["quota"].get("daily_budget_runs", 80))


def cooldowns():
    """{agent: paused-until timestamp}, past entries included."""
    return state.read_json(state.path("cooldowns.json"), {})


def cooling(agent, now=None):
    """True while the agent is in cooldown after quota or other failures."""
    return cooldowns().get(agent, 0) > (now or time.time())


def headroom(agent, cfg, now=None):
    now = now or time.time()
    acfg = cfg["routing"]["quota"]["agents"][agent]
    if cooling(agent, now):
        return 0.0
    src = acfg["quota"]["source"]
    try:
        if src == "claude_statusline":
            return claude(now)
        if src == "codex_rollout":
            return codex(now)
        if src == "copilot_api":
            return copilot(now, acfg)
        return self_metered(agent, now, acfg)
    except Exception as e:
        state.log("quota read failed", agent=agent, error=str(e))
        return None


def cooldown(agent, minutes):
    with state.locked("cooldowns"):
        p = state.path("cooldowns.json")
        d = state.read_json(p, {})
        d[agent] = time.time() + minutes * 60
        state.write_json(p, d)
