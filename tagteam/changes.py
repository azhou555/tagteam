"""Detect what a turn changed by diffing working-tree snapshots, not by trusting tool names."""
import fnmatch
import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile


def git(repo, *args, env=None, check=True):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def repo_root(cwd):
    r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def snapshot(repo):
    """Tree id of the full working state (tracked + untracked, minus ignored), user index untouched.

    Seeding the temp index from the real one means only dirty files get re-hashed.
    """
    with tempfile.TemporaryDirectory() as d:
        idx = os.path.join(d, "index")
        real = git(repo, "rev-parse", "--git-path", "index").strip()
        real = real if os.path.isabs(real) else os.path.join(repo, real)
        if os.path.exists(real):
            shutil.copyfile(real, idx)
        env = {"GIT_INDEX_FILE": idx}
        git(repo, "add", "-A", env=env)
        return git(repo, "write-tree", env=env).strip()


def ignored(rel, patterns):
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) for p in patterns)


def diff_trees(repo, base, head, ignore):
    """[{path, status, lines_changed, diff}] between two tree ids."""
    lines = {}
    for rec in git(repo, "diff", "--numstat", "-z", "--no-renames", base, head).split("\0"):
        if rec:
            a, d, rel = rec.split("\t", 2)
            lines[rel] = None if a == "-" else int(a) + int(d)      # None = binary
    status = {}
    fields = git(repo, "diff", "--name-status", "-z", "--no-renames", base, head).split("\0")
    for code, rel in zip(fields[::2], fields[1::2]):
        status[rel] = {"A": "added", "D": "deleted"}.get(code, "modified")
    return [{"path": rel, "status": status.get(rel, "modified"), "lines_changed": n,
             "diff": git(repo, "diff", "--no-renames", base, head, "--", rel)}
            for rel, n in lines.items() if n is not None and not ignored(rel, ignore)]


def file_digest(p):
    try:
        return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def outside_changes(paths, digests, ignore):
    """Changes to files outside any repo (plans in ~/.claude/plans, non-git dirs).

    `digests` maps path -> digest at turn start (None if absent then).
    """
    out = []
    for p in paths:
        now = file_digest(p)
        if now == digests.get(p, "unseen") or ignored(p, ignore):
            continue
        try:
            text = pathlib.Path(p).read_text(errors="replace")
        except OSError:
            text = ""
        out.append({"path": p, "status": "deleted" if now is None else "modified",
                    "lines_changed": text.count("\n") + 1 if text else 0,
                    "diff": f"(full content of {p})\n{text}"})
    return out
