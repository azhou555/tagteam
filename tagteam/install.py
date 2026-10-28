"""Install: each harness merges its hook config (every entry carries TAG so uninstall and re-install touch
exactly ours; Orca's and other entries are left alone); this module adds the menu bar item and launcher."""
import difflib
import importlib.metadata
import json
import os
import pathlib
import select
import shlex
import shutil
import subprocess
import sys
import time

from . import config, harnesses, resources, state
from .harnesses import base

H = pathlib.Path.home()
SWIFTBAR_APP = pathlib.Path("/Applications/SwiftBar.app")
SWIFTBAR_DOMAIN = "com.ameba.SwiftBar"
SWIFTBAR_DIR = H / "SwiftBar"                                       # used only if SwiftBar has no folder yet
SWIFTBAR_OLD_DIR = state.HOME / "swiftbar"                          # earlier default; SwiftBar skips hidden dirs
SWIFTBAR_PLUGIN = "tagteam.2s.sh"                                   # SwiftBar reads the refresh interval from the name
SWIFTBAR_ENABLE = "swiftbar://enableplugin?name=tagteam"           # undoes a "Disable Plugin" SwiftBar remembers
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


PICK_KEYS = {"\x1b[A": "up", "\x1b[B": "down", "k": "up", "j": "down", " ": "toggle", "\n": "done", "\r": "done"}


def pick_step(n, cursor, chosen, key):
    """One keypress in the picker: the (cursor, chosen) after it. `chosen` holds indexes in the order picked."""
    if key in ("up", "down"):
        return (cursor + (1 if key == "down" else -1)) % n, chosen
    if key == "toggle":
        return cursor, [i for i in chosen if i != cursor] if cursor in chosen else chosen + [cursor]
    return cursor, chosen


def _key(fd):
    ch = os.read(fd, 1).decode(errors="ignore")
    while ch.startswith("\x1b") and len(ch) < 3 and select.select([fd], [], [], 0.05)[0]:
        ch += os.read(fd, 1).decode(errors="ignore")    # an arrow key is ESC [ A; a lone ESC is ignored
    return PICK_KEYS.get(ch)


def _pick(detected, preselected, notes):
    """Arrow keys move, space selects, enter saves. A pick's number is its place in the review order."""
    import termios
    import tty
    fd, out = sys.stdin.fileno(), sys.stdout
    cursor, chosen = 0, [detected.index(n) for n in preselected]
    print("Pick the agents that may review your changes. Each one you pick receives your diffs.\n"
          "↑/↓ move · space select · enter save. Numbers are the review order; pick none to save nothing.")

    def draw(first):
        out.write(("" if first else f"\x1b[{len(detected)}F") + "".join(
            f"\x1b[2K{'›' if i == cursor else ' '} [{chosen.index(i) + 1 if i in chosen else ' '}] {name:8}{notes.get(name, '')}\n"
            for i, name in enumerate(detected)))
        out.flush()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?25l")
        draw(True)
        while (key := _key(fd)) != "done":
            cursor, chosen = pick_step(len(detected), cursor, chosen, key)
            draw(False)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        out.write("\x1b[?25h")
        out.flush()
    return [detected[i] for i in chosen]


def ask_consent(detected, preselected, notes=None):
    """The picker on a terminal, else a numbered prompt on stdin. Returns the picked agents in order; none is []."""
    notes = notes or {}
    if sys.stdin.isatty() and sys.stdout.isatty():
        return _pick(detected, preselected, notes)
    print("These agents can review your changes. Each one you pick receives your diffs. "
          "Pick reviewers in order (e.g. 2,1), or press Enter for none:")
    for i, name in enumerate(detected, 1):
        print(f"  {i}. [{'x' if name in preselected else ' '}] {name:8}{notes.get(name, '')}")
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


SIGN_IN = {True: "credentials found", None: "sign-in unknown", False: "signed out"}    # logged_in is never False today


def reviewer_note(name, cfg):
    """What a reviewer runs with, from config: `model sonnet · credentials found`."""
    r = cfg.get("reviewers", {}).get(name, {})
    bits = [f"model {r['model']}" if r.get("model") else "default model"]
    if r.get("effort"):
        bits.append(f"{r['effort']} effort")
    return " · ".join(bits + [SIGN_IN[harnesses.get(name).logged_in()]])


def choose_reviewers(reviewers, yes):
    """The reviewer list to save as `reviewers.use`, or None to leave it as it is. Never derived from detection:
    the list comes from --reviewers or an answer at the prompt, and --yes alone picks none."""
    found = [n for n in harnesses.detected() if harnesses.get(n).reviewer]
    use = None
    if reviewers is None and not yes and found:
        cfg = config.load()
        old = cfg.get("reviewers", {})
        use = old.get("use")                          # None: never saved; [] was saved on purpose
        reviewers = ask_consent(found, [n for n in use if n in found] if use is not None
                                else [n for n in found if old.get(n, {}).get("enabled") is True],
                                {n: reviewer_note(n, cfg) for n in found})
    if reviewers or (reviewers == [] and use):        # deselecting a saved list at the prompt clears it
        return reviewers
    return None


def save_consent(reviewers, authors, dry_run=False):
    """Save who may review (`reviewers.use`; None leaves it) and whose turns are reviewed (`authors.review`)."""
    saved = {}
    if reviewers is not None:
        saved["reviewers"] = ("reviewers", "use", reviewers)
    elif not config.load().get("reviewers", {}).get("use"):
        print("no reviewer list saved: nothing is reviewed until you run "
              "`tagteam install --reviewers codex,claude` (names in your preferred order)")
    if authors is not None:
        saved["authors"] = ("authors", "review", authors)
    for table, key, items in saved.values():
        print(f"{'would save' if dry_run else 'saved'} {table}.{key} = {', '.join(items) or '(none): nothing is reviewed'}")
        if not dry_run:
            config.save_user(table, key, items)


def set_consent(reviewers, authors, yes, dry_run=False):
    save_consent(choose_reviewers(reviewers, yes), authors, dry_run)


INTRO = """tagteam {version}
When a coding agent finishes a turn that changed code, a plan or a design, a different agent reviews the change,
read-only. Large changes, plans and designs wait for the review; small ones are reviewed in the background.
Reviews run on this machine through the agent apps you already use. Your diffs go only to the reviewers you pick.
"""


def _version():
    try:
        return importlib.metadata.version("tagteam")
    except importlib.metadata.PackageNotFoundError:
        return "(from a checkout)"


def _tilde(p):
    p = str(p)
    return "~" + p[len(str(H)):] if p.startswith(str(H) + os.sep) else p


def _has_hooks(p):
    try:
        return base.TAG in p.read_text(errors="ignore")
    except OSError:
        return False


def agent_lines():
    for name, h in harnesses.ALL.items():
        if not h.detect():
            yield f"  {name:8}not installed"
            continue
        hooks = "tagteam hooks in place" if _has_hooks(h.hooks_path()) else "no tagteam hooks yet"
        yield f"  {name:8}installed · {SIGN_IN[h.logged_in()]} · {hooks}"


def plan(agents):
    """Everything install would change, worked out without writing: each agent's hook check, every hook file
    rendered (name -> (path, old text, new text)), whether the routing migration applies, and the menu bar line."""
    routing = config.migrate_routing(dry_run=True)
    cfg = config.load(note_legacy=True)
    stop_timeout = 2 * cfg["review"]["deadline_s"] + 60     # a wait for a background review, then our own
    entry_cmd = entry()
    files = {}
    for name, h in harnesses.ALL.items():
        if name in agents and h.author:
            rc = check_agent(name)
            if rc:
                print(f"{name}: kept existing hooks (check failed with exit {rc}; "
                      f"`{entry_cmd} hook {name} turn-end --check` shows why)")
                continue
            files[name] = h.author.install(entry_cmd, stop_timeout, True)
    return {"entry": entry_cmd, "stop_timeout": stop_timeout, "files": files, "routing": routing,
            "menubar": menubar_plan()}


def _same(old, new):
    try:
        return json.loads(old) == json.loads(new)
    except ValueError:
        return False


def describe(old, new):
    """One line for a hook file's change: hooks added or updated, and the Claude status line taken over."""
    try:
        o = json.loads(old) if old.strip() else {}
    except ValueError:
        o = None
    n = json.loads(new)

    def rest(d):
        return {k: v for k, v in d.items() if k != "statusLine"} if isinstance(d, dict) else d
    what = []
    if _same(old, new):
        return "already up to date"
    if rest(o) != rest(n):
        what.append("create it with tagteam's hooks" if not old else
                    "update tagteam's hooks" if base.TAG in old else "add tagteam's hooks")
    if isinstance(o, dict) and o.get("statusLine") != n.get("statusLine"):
        what.append("show tagteam in your status line (your own command still runs)")
    return "; ".join(what) or "already up to date"


def summary(p, reviewers, authors):
    rows = [(_tilde(path), describe(old, new)) for path, old, new in p["files"].values()]
    current = config.load().get("reviewers", {}).get("use")
    settings = ['routing.mode = "quota" (keeps how your existing config routes reviews)'] if p["routing"] else []
    settings.append(f"reviewers.use = {', '.join(reviewers) or '(none): nothing is reviewed'}"
                    if reviewers is not None and reviewers != current
                    else f"reviewers.use stays {', '.join(current)}" if current
                    else "reviewers.use not set: nothing is reviewed until you pick reviewers")
    if authors is not None:
        settings.append(f"authors.review = {', '.join(authors) or '(none)'}")
    rows += [(_tilde(state.HOME / "config.toml") if i == 0 else "", s) for i, s in enumerate(settings)]
    rows.append(("menu bar", p["menubar"]))
    w = max(len(label) for label, _ in rows) + 3
    return ("\ntagteam install will:\n" + "".join(f"  {label:{w}}{text}\n" for label, text in rows)
            + "Each file is copied beside itself as <name>.tagteam-bak-<time> before it changes.")


def diffs(p):
    out = []
    for path, old, new in p["files"].values():
        out += difflib.unified_diff(old.splitlines(), new.splitlines(), str(path), f"{path} (after install)",
                                    lineterm="")
    return "\n".join(out)


def confirm():
    print("\nApply? [Y/n] ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("", "y", "yes")


def apply(p):
    """Write what `plan` worked out: the routing migration, each hook file (atomic, backed up), then the menu bar.
    A hook failure raises; a menu bar failure is reported and leaves the hooks in place."""
    if p["routing"] and config.migrate_routing():
        print('kept quota routing for your existing config (routing.mode = "quota")')
    for name, (path, old, new) in p["files"].items():
        if not _same(old, new):                           # an up-to-date file keeps its bytes and gets no backup
            harnesses.get(name).author.install(p["entry"], p["stop_timeout"], False)
    try:
        install_swiftbar()
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"menu bar: failed ({e}). Hooks are in place, so reviews still run; re-run install to retry.")
    from . import live
    n = live.backfill()
    if n:
        print(f"seeded {n} past reviews into {state.path('runs.jsonl')}")


def install(agents):
    """Hooks and menu bar for `agents`, with no prompts."""
    apply(plan(agents))


def next_steps(p):
    cfg = config.load()
    use = cfg.get("reviewers", {}).get("use") or []
    print("\nNext:")
    codex = p["files"].get("codex")
    if codex and codex[1] != codex[2]:
        print("  - Codex: open `codex`, run /hooks, and approve tagteam's 3 hooks. Codex runs no new hook until "
              "you do.")
    author = next((a for a in p["files"] if a in cfg["authors"]["review"] and any(r != a for r in use)), None)
    if author:
        print(f"  - Try it: ask {author} for a change of {cfg['triggers']['min_changed_lines']}+ lines. When its "
              f"turn ends, {' or '.join(r for r in use if r != author)} reviews it; "
              "`tagteam watch` or the menu bar shows it live.")
    elif not use:
        print("  - Nothing is reviewed yet: run `tagteam install` again and pick reviewers.")
    print(f"  - Settings: {_tilde(state.HOME / 'config.toml')} (`tagteam config` shows every value). "
          "Re-run `tagteam install` to change reviewers; `tagteam uninstall` removes everything.")
    from . import doctor
    print("\nHealth check (`tagteam doctor`):")
    doctor.run(False, False)


def run(agents, reviewers, authors, yes, dry_run):
    """`tagteam install`: say what tagteam is, choose reviewers, show the plan, confirm, apply, say what's next.
    Nothing is written before the confirm. Returns the exit code."""
    print(INTRO.format(version=_version()))
    print("\n".join(agent_lines()) + "\n")
    found = harnesses.detected()
    if len(found) < 2:
        print(f"tagteam needs two agents: one writes, a different one reviews. Installed: {', '.join(found) or 'none'}.\n"
              f"Install and sign in to another of {', '.join(harnesses.ALL)}, then run `tagteam install` again.")
        return 1
    try:
        use = choose_reviewers(reviewers, yes)
        p = plan(agents)
        print(summary(p, use, authors))
        if dry_run:
            print("\n" + (diffs(p) or "no hook file changes") + "\n\n(dry run: nothing was written)")
            return 0
        if not yes and not confirm():
            print("nothing was changed")
            return 1
    except KeyboardInterrupt:
        print("\ncancelled: nothing was changed")
        return 130
    try:
        apply(p)
    except (OSError, ValueError) as e:
        print(f"\ninstall stopped: {e}\nFiles updated above were backed up first (names shown). Fix the cause and "
              "run `tagteam install` again; it replaces only tagteam's own entries.")
        return 1
    save_consent(use, authors)
    next_steps(p)
    return 0


def _swiftbar_dir():
    if state.extras_off() or not shutil.which("defaults"):                # not macOS: no SwiftBar
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
            "# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>\n"
            f"{SWIFTBAR_MARK}\n"
            f"{env}exec {entry()} swiftbar\n")


def _ours_plugin(p):
    try:
        return SWIFTBAR_MARK.encode() in p.read_bytes()             # bytes: a binary plugin is simply not ours
    except OSError:
        return False


def _menubar():
    """Where the plugin goes, as (folder, SwiftBar's current folder or None), or why the menu bar is skipped."""
    if state.extras_off():
        return "skipped (TAGTEAM_NO_MACOS_EXTRAS is set)"
    if not SWIFTBAR_APP.exists():
        return "skipped. SwiftBar isn't installed (macOS only: brew install --cask swiftbar, then re-run install)"
    current = _swiftbar_dir()
    legacy = current == SWIFTBAR_OLD_DIR
    if current and not legacy and any(part.startswith(".") for part in current.parts):
        return (f"skipped. SwiftBar ignores hidden folders, and its plugin folder {current} is inside one; "
                "pick a visible folder in SwiftBar's preferences, then re-run install")
    target = SWIFTBAR_DIR if current is None or legacy else current
    plugin = target / SWIFTBAR_PLUGIN
    if plugin.exists() and not _ours_plugin(plugin):
        return f"skipped. {plugin} exists and isn't tagteam's"
    return target, current


def menubar_plan():
    m = _menubar()
    if isinstance(m, str):
        return m
    target, current = m
    return (f"write {_tilde(target / SWIFTBAR_PLUGIN)}"
            + (" and restart SwiftBar so it loads that folder" if target != current else "")
            + f"; add {_tilde(LAUNCHER)} so Spotlight finds tagteam")


def install_swiftbar():
    """Menu bar item via SwiftBar, if it's installed. Uses the user's plugin folder, or ~/SwiftBar if none.

    Order matters: the plugin is written before SwiftBar is repointed or the legacy copy is removed, so a
    failure leaves the previous setup working.
    """
    m = _menubar()
    if isinstance(m, str):
        print(f"menu bar: {m}")
        return
    target, current = m
    legacy = current == SWIFTBAR_OLD_DIR
    plugin = target / SWIFTBAR_PLUGIN
    target.mkdir(parents=True, exist_ok=True)
    plugin.write_text(swiftbar_plugin())
    plugin.chmod(0o755)
    if target != current:
        subprocess.run(["defaults", "write", SWIFTBAR_DOMAIN, "PluginDirectory", str(target)], check=True)
        if legacy and _ours_plugin(current / SWIFTBAR_PLUGIN):
            (current / SWIFTBAR_PLUGIN).unlink()
        subprocess.run(["pkill", "-x", "SwiftBar"], check=False)    # SwiftBar reads its folder only at launch
        for _ in range(25):
            if not _swiftbar_running():
                break
            time.sleep(0.2)
    if not _swiftbar_running():                      # opening a running SwiftBar pops "SwiftBar is already running"
        subprocess.run(["open", "-g", "-a", str(SWIFTBAR_APP)], check=False)
    subprocess.run(["open", "-g", SWIFTBAR_ENABLE], check=False)
    subprocess.run(["open", "-g", "swiftbar://refreshallplugins"], check=False)
    print(f"menu bar: wrote {plugin}")
    install_launcher()


def relaunch_sh():
    """The launcher's restart. Opening a running SwiftBar pops "SwiftBar is already running", so wait until it quit."""
    return ("pkill -x SwiftBar; for i in $(seq 25); do pgrep -qx SwiftBar || break; sleep 0.2; done; "
            f"pgrep -qx SwiftBar || open -g -a '{SWIFTBAR_APP}'; open -g '{SWIFTBAR_ENABLE}'")


def _swiftbar_running():
    return subprocess.run(["pgrep", "-qx", "SwiftBar"], check=False).returncode == 0


def install_launcher():
    """A tiny app so Spotlight finds "tagteam": it starts SwiftBar (or refreshes it if running)."""
    if LAUNCHER.exists() and not (LAUNCHER / LAUNCHER_MARK).exists():
        print(f"launcher: skipped. {LAUNCHER} exists and isn't tagteam's")
        return
    # Always a clean restart: a running SwiftBar can be stuck with no items, and `open` on a running one pops
    # "SwiftBar is already running", so wait until it has quit. Then re-enable the plugin: SwiftBar keeps a
    # disabled plugin hidden across restarts.
    script = (f'do shell script "{relaunch_sh()}"\n'
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
    if not state.extras_off() and (LAUNCHER / LAUNCHER_MARK).exists():
        shutil.rmtree(LAUNCHER)
        print(f"removed {LAUNCHER}")
