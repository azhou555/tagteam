"""Install: each harness merges its hook config (every entry carries TAG so uninstall and re-install touch
exactly ours; Orca's and other entries are left alone); this module adds the menu bar item and launcher."""
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time

from . import config, harnesses, resources, state

H = pathlib.Path.home()
SWIFTBAR_APP = pathlib.Path("/Applications/SwiftBar.app")
SWIFTBAR_DOMAIN = "com.ameba.SwiftBar"
SWIFTBAR_DIR = H / "SwiftBar"                                       # used only if SwiftBar has no folder yet
SWIFTBAR_OLD_DIR = state.HOME / "swiftbar"                          # earlier default; SwiftBar skips hidden dirs
SWIFTBAR_PLUGIN = "tagteam.2s.sh"                                   # SwiftBar reads the refresh interval from the name
LAUNCHER = H / "Applications" / "tagteam.app"                       # Spotlight: type "tagteam" to open the menu bar item
LAUNCHER_MARK = "Contents/Resources/tagteam-launcher"               # ownership marker inside the bundle
SWIFTBAR_MARK = "# Written by `tagteam install`; removed by `tagteam uninstall`."


def entry_argv():
    """argv that runs this tagteam: the installed executable, else this script, else `python -m tagteam`."""
    found = shutil.which("tagteam")
    if found:
        return [os.path.abspath(found)]
    me = os.path.realpath(sys.argv[0])
    if os.path.isfile(me) and os.access(me, os.X_OK):
        return [me]
    return [sys.executable, "-m", "tagteam"]                        # never point hooks at an unexecutable path


def entry():
    """The same, as a shell command prefix."""
    return shlex.join(entry_argv())


def check_agent(name):
    """Run the real hook parser on the bundled Stop payload in a child; 0 means this install can parse it."""
    payload = resources.path(f"fixtures/{name}-stop.json").read_text()
    try:
        r = subprocess.run([*entry_argv(), "hook", name, "turn-end", "--check"], input=payload,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  {e}", file=sys.stderr)
        return 1
    if r.returncode:
        print(f"  {r.stderr.strip()}", file=sys.stderr)
    return r.returncode


def ask_consent(detected, preselected):
    """Numbered prompt on stdin; returns the picked agents in the order typed. Nothing typed means none."""
    print("These agents can review your changes. Each one you pick receives your diffs. "
          "Pick reviewers in order (e.g. 2,1), or press Enter for none:")
    for i, name in enumerate(detected, 1):
        print(f"  {i}. [{'x' if name in preselected else ' '}] {name}")
    if preselected:
        print("  ([x] = enabled in your old config; type their numbers to keep them)")
    while True:
        line = sys.stdin.readline()
        if not line.strip():
            return []
        try:
            nums = [int(t) for t in line.replace(",", " ").split()]
            if not all(1 <= n <= len(detected) for n in nums):
                raise ValueError
        except ValueError:
            print(f"Enter numbers 1-{len(detected)} separated by commas, or press Enter for none:")
            continue
        return list(dict.fromkeys(detected[n - 1] for n in nums))


def set_consent(reviewers, authors, yes, dry_run=False):
    """Save who may review (`reviewers.use`) and whose turns are reviewed. Never derived from detection:
    the list comes from --reviewers or an answer at the prompt, and --yes alone saves none."""
    found = [n for n in harnesses.detected() if harnesses.get(n).reviewer]
    if reviewers is None and not yes and found:
        old = config.load().get("reviewers", {})
        reviewers = ask_consent(found, [n for n in found if old.get(n, {}).get("enabled") is True])
    if reviewers:
        saved = {"reviewers": ("reviewers", "use", reviewers)}
    else:
        saved = {}
        print("no reviewer list saved: nothing is reviewed until you run "
              "`tagteam install --reviewers codex,claude` (names in your preferred order)")
    if authors is not None:
        saved["authors"] = ("authors", "review", authors)
    for table, key, items in saved.values():
        print(f"{'would save' if dry_run else 'saved'} {table}.{key} = {', '.join(items)}")
        if not dry_run:
            config.save_user(table, key, items)


def install(agents, dry_run=False):
    cfg = config.load()
    stop_timeout = cfg["review"]["deadline_s"] + 60
    entry_cmd = entry()
    for name, h in harnesses.ALL.items():
        if name in agents and h.author:
            rc = check_agent(name)
            if rc:
                print(f"{name}: kept existing hooks (check failed with exit {rc}; "
                      f"`{entry_cmd} hook {name} turn-end --check` shows why)")
                continue
            h.author.install(entry_cmd, stop_timeout, dry_run)
    install_swiftbar(dry_run)
    if not dry_run:
        from . import live
        n = live.backfill()
        if n:
            print(f"seeded {n} past reviews into {state.path('runs.jsonl')}")
    if "codex" in agents and not dry_run:
        print("\nCodex: open `codex`, run /hooks, and approve the 3 tagteam hooks (Codex will not run "
              "new hooks until you trust them).")


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
            f"{env}exec {entry()} swiftbar\n")


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
    for h in harnesses.ALL.values():
        if h.author:
            h.author.uninstall()
    d = _swiftbar_dir()
    if d and _ours_plugin(d / SWIFTBAR_PLUGIN):
        (d / SWIFTBAR_PLUGIN).unlink()
        print(f"removed {d / SWIFTBAR_PLUGIN}")
    if (LAUNCHER / LAUNCHER_MARK).exists():
        shutil.rmtree(LAUNCHER)
        print(f"removed {LAUNCHER}")
