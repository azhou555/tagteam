"""Merge-only install into each agent's hook config. Every entry we add carries TAG so
uninstall (and re-install) touches exactly ours; Orca's and other entries are left alone."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

from . import state

TAG = "tagteam-hook-form=1"
H = pathlib.Path.home()
CLAUDE = H / ".claude" / "settings.json"
CODEX = H / ".codex" / "hooks.json"
COPILOT = H / ".copilot" / "hooks" / "tagteam.json"
AGY = H / ".gemini" / "config" / "hooks.json"
STATUSLINE_ORIG = state.HOME / "statusline.orig"
STATUSLINE_ORIG_REFRESH = state.HOME / "statusline.orig-refresh"   # user's refreshInterval before ours, or empty
REFRESH_S = 2                                                       # ticks the status line during a blocking review
TOUCH_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash"
SWIFTBAR_APP = pathlib.Path("/Applications/SwiftBar.app")
SWIFTBAR_DOMAIN = "com.ameba.SwiftBar"
SWIFTBAR_DIR = H / "SwiftBar"                                       # used only if SwiftBar has no folder yet
SWIFTBAR_OLD_DIR = state.HOME / "swiftbar"                          # earlier default; SwiftBar skips hidden dirs
SWIFTBAR_PLUGIN = "tagteam.2s.sh"                                   # SwiftBar reads the refresh interval from the name
LAUNCHER = H / "Applications" / "tagteam.app"                       # Spotlight: type "tagteam" to open the menu bar item
LAUNCHER_MARK = "Contents/Resources/tagteam-launcher"               # ownership marker inside the bundle
SWIFTBAR_MARK = "# Written by `tagteam install`; removed by `tagteam uninstall`."


def cmd(agent, phase):
    return f": {TAG}; '{sys.executable}' '{state.ROOT / 'bin' / 'tagteam'}' hook {agent} {phase}"


def _ours(entry):
    return TAG in json.dumps(entry)


def _load(p):
    return json.loads(p.read_text()) if p.exists() else {}


def _save(p, data, dry_run):
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


def _strip(hooks):
    for event in list(hooks):
        if isinstance(hooks[event], list):
            hooks[event] = [e for e in hooks[event] if not _ours(e)]
            if not hooks[event]:
                del hooks[event]


def claude_shaped(agent, stop_timeout):
    def group(phase, timeout, matcher=None):
        g = {"hooks": [{"type": "command", "command": cmd(agent, phase), "timeout": timeout}]}
        return {"matcher": matcher, **g} if matcher else g
    events = {
        "UserPromptSubmit": group("turn-start", 30),
        "PostToolUse": group("touch", 30, TOUCH_MATCHER if agent == "claude" else None),
        "Stop": group("turn-end", stop_timeout),
    }
    if agent == "claude":
        events["PreToolUse"] = group("plan-gate", stop_timeout, "ExitPlanMode")
    return events


def install(agents, dry_run=False):
    cfg = state.load_config()
    stop_timeout = cfg["review"]["deadline_s"] + 60
    for agent, p in (("claude", CLAUDE), ("codex", CODEX)):
        if agent not in agents:
            continue
        data = _load(p)
        hooks = data.setdefault("hooks", {})
        _strip(hooks)
        for event, entry in claude_shaped(agent, stop_timeout).items():
            hooks.setdefault(event, []).append(entry)
        if agent == "claude":
            _wrap_statusline(data, dry_run)
        _save(p, data, dry_run)
    if "copilot" in agents:
        def h(phase, t):
            return [{"type": "command", "bash": cmd("copilot", phase), "timeoutSec": t}]
        _save(COPILOT, {"version": 1, "hooks": {"userPromptSubmitted": h("turn-start", 30),
                                                "postToolUse": h("touch", 30),
                                                "agentStop": h("turn-end", stop_timeout)}}, dry_run)
    if "agy" in agents:
        data = _load(AGY)
        data["tagteam"] = {
            "PreInvocation": [{"type": "command", "command": cmd("agy", "turn-start"), "timeout": 30}],
            "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": cmd("agy", "touch"),
                                                        "timeout": 30}]}],
            "Stop": [{"type": "command", "command": cmd("agy", "turn-end"), "timeout": stop_timeout}],
        }
        _save(AGY, data, dry_run)
    install_swiftbar(dry_run)
    if not dry_run:
        from . import live
        n = live.backfill()
        if n:
            print(f"seeded {n} past reviews into {state.path('runs.jsonl')}")
    if "codex" in agents and not dry_run:
        print("\nCodex: open `codex`, run /hooks, and approve the 3 tagteam hooks (Codex will not run "
              "new hooks until you trust them).")


def _wrap_statusline(data, dry_run):
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


def _swiftbar_dir():
    if not shutil.which("defaults"):                                # not macOS: no SwiftBar
        return None
    r = subprocess.run(["defaults", "read", SWIFTBAR_DOMAIN, "PluginDirectory"], capture_output=True, text=True)
    return pathlib.Path(os.path.expanduser(r.stdout.strip())) if r.returncode == 0 and r.stdout.strip() else None


def swiftbar_plugin():
    env = f"TAGTEAM_HOME='{os.environ['TAGTEAM_HOME']}' " if os.environ.get("TAGTEAM_HOME") else ""
    return ("#!/bin/sh\n"
            "# <swiftbar.title>tagteam</swiftbar.title>\n"
            "# <swiftbar.hideAbout>true</swiftbar.hideAbout>\n"
            "# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>\n"
            "# <swiftbar.hideLastUpdated>true</swiftbar.hideLastUpdated>\n"
            f"{SWIFTBAR_MARK}\n"
            f"{env}PYTHONPATH='{state.ROOT}' exec '{sys.executable}' -m tagteam.live swiftbar\n")


def _ours_plugin(p):
    try:
        return SWIFTBAR_MARK.encode() in p.read_bytes()             # bytes: a binary plugin is simply not ours
    except OSError:
        return False


def install_swiftbar(dry_run=False):
    """Menu bar item via SwiftBar, if it's installed. Uses the user's plugin folder, or ~/SwiftBar if none.

    Order matters: the plugin is written before SwiftBar is repointed or the legacy copy is removed, so a
    failure leaves the previous setup working.
    """
    if not SWIFTBAR_APP.exists():
        print("SwiftBar not installed; skipping menu bar item (brew install --cask swiftbar, then re-run install)")
        return
    current = _swiftbar_dir()
    legacy = current == SWIFTBAR_OLD_DIR
    if current and not legacy and any(part.startswith(".") for part in current.parts):
        print(f"menu bar: skipped. SwiftBar ignores hidden folders, and its plugin folder {current} is inside one; "
              "pick a visible folder in SwiftBar's preferences, then re-run install")
        return
    target = SWIFTBAR_DIR if current is None or legacy else current
    plugin = target / SWIFTBAR_PLUGIN
    if plugin.exists() and not _ours_plugin(plugin):
        print(f"menu bar: skipped. {plugin} exists and isn't tagteam's")
        return
    if dry_run:
        print(f"--- {plugin} (dry run)\n{swiftbar_plugin()}")
        return
    target.mkdir(parents=True, exist_ok=True)
    plugin.write_text(swiftbar_plugin())
    plugin.chmod(0o755)
    if target != current:
        subprocess.run(["defaults", "write", SWIFTBAR_DOMAIN, "PluginDirectory", str(target)], check=True)
        if legacy and _ours_plugin(current / SWIFTBAR_PLUGIN):
            (current / SWIFTBAR_PLUGIN).unlink()
        subprocess.run(["pkill", "-x", "SwiftBar"], check=False)    # SwiftBar reads its folder only at launch
        time.sleep(1)
    subprocess.run(["open", "-g", "-a", str(SWIFTBAR_APP)], check=False)
    subprocess.run(["open", "-g", "swiftbar://refreshallplugins"], check=False)
    print(f"menu bar: wrote {plugin}")
    install_launcher()


def install_launcher():
    """A tiny app so Spotlight finds "tagteam": it starts SwiftBar (or refreshes it if running)."""
    if LAUNCHER.exists() and not (LAUNCHER / LAUNCHER_MARK).exists():
        print(f"launcher: skipped. {LAUNCHER} exists and isn't tagteam's")
        return
    # Always a clean restart: a running SwiftBar can be stuck with no items, and `open` on a running one only
    # says it's already open.
    script = (f'do shell script "pkill -x SwiftBar; sleep 1; open -g -a \'{SWIFTBAR_APP}\'"\n'
              'display notification "Look for the sage two-person icon in the menu bar." with title "tagteam"')
    shutil.rmtree(LAUNCHER, ignore_errors=True)
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["osacompile", "-o", str(LAUNCHER), "-e", script], check=True, capture_output=True)
    (LAUNCHER / LAUNCHER_MARK).touch()
    subprocess.run(["mdimport", str(LAUNCHER)], check=False, capture_output=True)   # index it for Spotlight now
    print(f"launcher: wrote {LAUNCHER} (Spotlight: tagteam)")


def uninstall():
    for p in (CLAUDE, CODEX):
        if p.exists():
            data = _load(p)
            _strip(data.get("hooks", {}))
            if p == CLAUDE and "tagteam-statusline" in (data.get("statusLine") or {}).get("command", ""):
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
            _save(p, data, False)
    if COPILOT.exists():
        COPILOT.unlink()
        print(f"removed {COPILOT}")
    if AGY.exists():
        data = _load(AGY)
        if data.pop("tagteam", None) is not None:
            _save(AGY, data, False)
    d = _swiftbar_dir()
    if d and _ours_plugin(d / SWIFTBAR_PLUGIN):
        (d / SWIFTBAR_PLUGIN).unlink()
        print(f"removed {d / SWIFTBAR_PLUGIN}")
    if (LAUNCHER / LAUNCHER_MARK).exists():
        shutil.rmtree(LAUNCHER)
        print(f"removed {LAUNCHER}")
