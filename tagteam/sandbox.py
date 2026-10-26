"""Run reviewers in a disposable checkout of the turn's snapshot, and detect writes that reach the real tree."""
import contextlib
import os
import shutil
import tempfile

from . import changes, state

_GIT_ENV = {"GIT_AUTHOR_NAME": "tagteam", "GIT_COMMITTER_NAME": "tagteam",
            "GIT_AUTHOR_EMAIL": "tagteam@localhost", "GIT_COMMITTER_EMAIL": "tagteam@localhost",
            "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000"}


@contextlib.contextmanager
def checkout(repo, tree, cwd=None):
    """Yield a throwaway worktree of `tree` (removed on exit, whatever happens), or `cwd` when there is no repo
    or the worktree can't be made: those runs rely on the CLI's read-only flags plus `wrote`."""
    cwd = cwd or os.getcwd()
    path = None
    if repo and tree:
        try:
            commit = changes.git(repo, "commit-tree", tree, "-m", "tagteam-review", env=_GIT_ENV).strip()
            path = os.path.realpath(tempfile.mkdtemp(prefix="tagteam-review-"))
            changes.git(repo, "worktree", "add", "--detach", path, commit)
        except (RuntimeError, OSError) as e:
            state.log("sandbox unavailable, reviewing in place", repo=repo, error=repr(e))
            if path:
                shutil.rmtree(path, ignore_errors=True)
            path = None
    try:
        yield path or cwd
    finally:
        if path:
            try:
                changes.git(repo, "worktree", "remove", "--force", path)
            except (RuntimeError, OSError) as e:
                state.log("sandbox worktree not removed cleanly", path=path, error=repr(e))
            shutil.rmtree(path, ignore_errors=True)


def wrote(repo, before_tree, touched_digests_before):
    """True if the real tree or any touched outside-repo file differs from before the review."""
    if any(changes.file_digest(p) != d for p, d in touched_digests_before.items()):
        return True
    if not (repo and before_tree):
        return False
    try:
        return changes.snapshot(repo) != before_tree
    except (RuntimeError, OSError) as e:
        state.log("real tree not re-checked", repo=repo, error=repr(e))
        return False
