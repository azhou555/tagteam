"""Run each reviewer in its own disposable checkout of the turn's snapshot, and detect writes that escape it."""
import concurrent.futures as cf
import contextlib
import json
import hashlib
import os
import shutil
import tempfile
import time

from . import changes, state

PREFIX = "tagteam-review-"
# Copilot runs a trusted repo's own hooks (.claude/settings.json, .github/hooks) and MCP servers, even in -p and in a
# worktree of it.
COPILOT_SETTINGS = ".github/copilot/settings.local.json"
COPILOT_MCP = (".mcp.json", ".github/mcp.json")
_GIT_ENV = {"GIT_AUTHOR_NAME": "tagteam", "GIT_COMMITTER_NAME": "tagteam",
            "GIT_AUTHOR_EMAIL": "tagteam@localhost", "GIT_COMMITTER_EMAIL": "tagteam@localhost",
            "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}


def _remove(repo, path):
    try:
        changes.git(repo, "worktree", "remove", "-f", "-f", path)
    except (RuntimeError, OSError) as e:
        state.log("sandbox worktree not removed cleanly", path=path, error=repr(e))
    shutil.rmtree(path, ignore_errors=True)


def _clear_stale(repo, stale_s):
    """Drop this tool's own tagteam-review-* leftovers (a killed review): missing, or idle past `stale_s`,
    which must exceed any live review's deadline."""
    cur = None
    for line in changes.git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:], "prunable": False}
        elif line.startswith("prunable") and cur:
            cur["prunable"] = True
        elif not line and cur:
            p = cur["path"]
            if os.path.basename(p).startswith(PREFIX):
                try:
                    old = time.time() - os.path.getmtime(p) > stale_s
                except OSError:
                    old = True
                if cur["prunable"] or old:
                    _remove(repo, p)
            cur = None


def _worktree(repo, commit):
    """Register an empty worktree. Run these one at a time: `worktree add` reads every sibling's admin files,
    and a concurrent add can catch one half-written ("failed to read .git/worktrees/<name>/commondir")."""
    path = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX))
    if changes.in_repo(path, repo):
        shutil.rmtree(path, ignore_errors=True)
        raise OSError(f"temp dir {path} is inside the repo")
    try:
        changes.git(repo, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--no-checkout", "--detach", path, commit)
    except (RuntimeError, OSError):
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def _json_dict(p):
    try:
        with open(p) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _no_repo_code(path):
    """Turn off what Copilot would start from the reviewed repo: all hooks (its own settings.json can't undo it)
    and the repo's MCP servers, which Copilot reads from .mcp.json or else .github/mcp.json."""
    p = os.path.join(path, COPILOT_SETTINGS)
    if not os.path.realpath(p).startswith(path + os.sep):       # a symlink in the repo would write outside
        state.log("copilot hooks and MCP left on in sandbox: settings path leaves it", path=path)
        return
    cur = _json_dict(p)
    servers = [n for f in COPILOT_MCP for n in _json_dict(os.path.join(path, f)).get("mcpServers") or {}]
    off = cur.get("disabledMcpServers") if isinstance(cur.get("disabledMcpServers"), list) else []
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({**cur, "disableAllHooks": True, "disabledMcpServers": list(dict.fromkeys(off + servers))}, f)


def _populate(path):
    """Check out the files; touches only this worktree's index and tree, so these run in parallel."""
    changes.git(path, "-c", "core.hooksPath=/dev/null", "reset", "-q", "--hard", env={"GIT_LFS_SKIP_SMUDGE": "1"})
    _no_repo_code(path)


@contextlib.contextmanager
def checkouts(repo, tree, names, cwd=None, stale_s=7200):
    """Yield {name: path}: one throwaway worktree of `tree` per name, all removed on exit whatever happens.
    Without a repo, or if worktrees can't be made, every name gets `cwd` (flags plus `Watch` only)."""
    cwd = cwd or os.getcwd()
    made, lock = [], None
    try:
        if repo and tree:
            try:
                lock = _registry_lock(repo)
                with state.locked(lock):
                    _clear_stale(repo, stale_s)
                    commit = changes.git(repo, "commit-tree", tree, "-m", "tagteam-review", env=_GIT_ENV).strip()
                    for _ in names:
                        made.append(_worktree(repo, commit))
                with cf.ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
                    list(ex.map(_populate, made))
            except (RuntimeError, OSError) as e:
                state.log("sandbox unavailable, reviewing in place", repo=repo, error=repr(e))
                _remove_all(repo, lock, made)
                made = []
        yield dict(zip(names, made)) if made else {n: cwd for n in names}
    finally:
        _remove_all(repo, lock, made)


def _registry_lock(repo):
    """Lock name for `repo`'s .git/worktrees. Other tagteam processes (another session, a background review) add
    and remove worktrees of the same repo, and git doesn't make that safe: see `_worktree`."""
    return "worktrees-" + hashlib.sha256(os.path.realpath(_common_dir(repo)).encode()).hexdigest()[:16]


def _remove_all(repo, lock, paths):
    if paths:
        with state.locked(lock):
            for p in paths:
                _remove(repo, p)


@contextlib.contextmanager
def checkout(repo, tree, cwd=None):
    with checkouts(repo, tree, ["_"], cwd) as m:
        yield m["_"]


def _common_dir(repo):
    common = changes.git(repo, "rev-parse", "--git-common-dir").strip()
    return common if os.path.isabs(common) else os.path.join(repo, common)


def _config(path):
    """(entries that could run code or redirect git, branch.* tracking entries): `key=value` lines. Branch
    tracking is what VS Code and `git push -u` write mid-review; nothing in it runs code. Unparseable config
    is one opaque entry, so any change to it still counts."""
    try:
        lines = changes.git(os.path.dirname(path), "config", "--file", path, "--list").splitlines()
    except (RuntimeError, OSError):
        return {f"(unparseable config {changes.file_digest(path)})"}, set()
    return {l for l in lines if not l.startswith("branch.")}, {l for l in lines if l.startswith("branch.")}


def _git_files(repo):
    """The shared git dir's config entries and hook digests: what a sandboxed `git config` or hook install
    changes. Returns ({config entry or hook path: digest-or-True}, branch tracking entries)."""
    common = _common_dir(repo)
    risky, tracking = _config(os.path.join(common, "config"))
    out = dict.fromkeys(risky, True)
    for root, _, files in os.walk(os.path.join(common, "hooks")):
        out.update({os.path.join(root, f): changes.file_digest(os.path.join(root, f)) for f in files})
    return out, tracking


def _delta(old, new):
    return sorted(f"+{k}" if k in new else f"-{k}" for k in set(old) ^ set(new)) + sorted(
        k for k in set(old) & set(new) if old[k] != new[k])


def _refs(repo):
    return {k: v for k, v in changes.refs(repo).items() if not k.startswith("refs/remotes/")}


def written(path, tree):
    """The paths in which a sandbox no longer matches `tree` (tagteam's own Copilot settings aside); raises if
    git can't tell."""
    now = changes.snapshot(path)
    if now == tree:
        return []
    wrote = changes.git(path, "diff", "--name-only", tree, now).split()
    return [p for p in wrote if p != COPILOT_SETTINGS] if wrote else ["(tree changed)"]


def dirty(path, tree):
    """`written`, but empty (and logged) if git can't tell."""
    try:
        return written(path, tree)
    except (RuntimeError, OSError) as e:
        state.log("sandbox not re-checked", path=path, error=repr(e))
        return []


class Watch:
    """What the review must not change. `after()` reports, in three classes of increasing doubt:
    git: shared config entries (`+key=value` / `-key=value`) and hook paths that changed (a security signal),
    outside: touched outside-repo files that changed, repo: {tree: [paths], refs: [names], config: [branch.*
    entries]} for the real working tree, non-remote refs and branch tracking (maybe the user's own doing)."""

    def __init__(self, repo, tree, outside_paths=()):
        self.repo, self.tree = repo, tree
        self.outside = {p: changes.file_digest(p) for p in outside_paths}
        self.git = self.tracking = self.refs = None
        if repo:
            try:
                (self.git, self.tracking), self.refs = _git_files(repo), _refs(repo)
            except (RuntimeError, OSError) as e:
                state.log("git state not watched", repo=repo, error=repr(e))

    def after(self):
        out = {"git": [], "outside": [p for p, d in self.outside.items() if changes.file_digest(p) != d],
               "repo": {"tree": [], "refs": [], "config": []}}
        if not self.repo:
            return out
        try:
            if self.git is not None:
                now, tracking = _git_files(self.repo)
                out["git"] = _delta(self.git, now)
                out["repo"]["config"] = _delta(dict.fromkeys(self.tracking, True), dict.fromkeys(tracking, True))
                refs = _refs(self.repo)
                out["repo"]["refs"] = sorted(k for k in set(refs) | set(self.refs) if refs.get(k) != self.refs.get(k))
            if self.tree:
                head = changes.snapshot(self.repo)
                if head != self.tree:
                    out["repo"]["tree"] = changes.git(self.repo, "diff", "--name-only", self.tree, head).split()
        except (RuntimeError, OSError) as e:
            state.log("real repo not re-checked", repo=self.repo, error=repr(e))
        return out
