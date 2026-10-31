"""Run each reviewer in its own disposable checkout of the turn's snapshot, and detect writes that escape it."""
import concurrent.futures as cf
import contextlib
import os
import shutil
import tempfile
import time

from . import changes, state

PREFIX = "tagteam-review-"
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


def _populate(path):
    """Check out the files; touches only this worktree's index and tree, so these run in parallel."""
    changes.git(path, "-c", "core.hooksPath=/dev/null", "reset", "-q", "--hard", env={"GIT_LFS_SKIP_SMUDGE": "1"})


@contextlib.contextmanager
def checkouts(repo, tree, names, cwd=None, stale_s=7200):
    """Yield {name: path}: one throwaway worktree of `tree` per name, all removed on exit whatever happens.
    Without a repo, or if worktrees can't be made, every name gets `cwd` (flags plus `Watch` only)."""
    cwd = cwd or os.getcwd()
    made = []
    try:
        if repo and tree:
            try:
                _clear_stale(repo, stale_s)
                commit = changes.git(repo, "commit-tree", tree, "-m", "tagteam-review", env=_GIT_ENV).strip()
                for _ in names:
                    made.append(_worktree(repo, commit))
                with cf.ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
                    list(ex.map(_populate, made))
            except (RuntimeError, OSError) as e:
                state.log("sandbox unavailable, reviewing in place", repo=repo, error=repr(e))
                for p in made:
                    _remove(repo, p)
                made = []
        yield dict(zip(names, made)) if made else {n: cwd for n in names}
    finally:
        for p in made:
            _remove(repo, p)


@contextlib.contextmanager
def checkout(repo, tree, cwd=None):
    with checkouts(repo, tree, ["_"], cwd) as m:
        yield m["_"]


def _git_files(repo):
    """Digests of the shared git dir's config and hooks: what a sandboxed `git config` or hook install changes."""
    common = changes.git(repo, "rev-parse", "--git-common-dir").strip()
    common = common if os.path.isabs(common) else os.path.join(repo, common)
    paths = [os.path.join(common, "config")]
    for root, _, files in os.walk(os.path.join(common, "hooks")):
        paths += [os.path.join(root, f) for f in files]
    return {p: changes.file_digest(p) for p in paths}


def _refs(repo):
    return {k: v for k, v in changes.refs(repo).items() if not k.startswith("refs/remotes/")}


def dirty(path, tree):
    """True if a sandbox no longer holds exactly `tree`."""
    try:
        return changes.snapshot(path) != tree
    except (RuntimeError, OSError) as e:
        state.log("sandbox not re-checked", path=path, error=repr(e))
        return False


class Watch:
    """What the review must not change. `after()` reports, in three classes of increasing doubt:
    git: shared config/hooks that changed (a security signal), outside: touched outside-repo files that changed,
    repo: {tree: [paths], refs: [names]} for the real working tree and non-remote refs (maybe the user's own edits)."""

    def __init__(self, repo, tree, outside_paths=()):
        self.repo, self.tree = repo, tree
        self.outside = {p: changes.file_digest(p) for p in outside_paths}
        self.git = self.refs = None
        if repo:
            try:
                self.git, self.refs = _git_files(repo), _refs(repo)
            except (RuntimeError, OSError) as e:
                state.log("git state not watched", repo=repo, error=repr(e))

    def after(self):
        out = {"git": [], "outside": [p for p, d in self.outside.items() if changes.file_digest(p) != d],
               "repo": {"tree": [], "refs": []}}
        if not self.repo:
            return out
        try:
            if self.git is not None:
                now = _git_files(self.repo)
                out["git"] = sorted(p for p in set(now) | set(self.git) if now.get(p) != self.git.get(p))
                refs = _refs(self.repo)
                out["repo"]["refs"] = sorted(k for k in set(refs) | set(self.refs) if refs.get(k) != self.refs.get(k))
            if self.tree:
                head = changes.snapshot(self.repo)
                if head != self.tree:
                    out["repo"]["tree"] = changes.git(self.repo, "diff", "--name-only", self.tree, head).split()
        except (RuntimeError, OSError) as e:
            state.log("real repo not re-checked", repo=self.repo, error=repr(e))
        return out
