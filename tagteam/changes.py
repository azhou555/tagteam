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


def in_repo(path, repo):
    """True if `path` is `repo` or inside it. Symlinks resolve on both sides: git reports the real
    top-level while agents report paths through whatever cwd they were started in."""
    if not repo:
        return False
    p, r = os.path.realpath(path), os.path.realpath(repo)
    return p == r or p.startswith(r + os.sep)


def snapshot(repo):
    """Tree id of the full working state (tracked + untracked, minus ignored), user index untouched.

    Seeding the temp index from the real one means only dirty files get re-hashed.
    """
    with tempfile.TemporaryDirectory() as d:
        idx = os.path.join(d, "index")
        real = git(repo, "rev-parse", "--git-path", "index").strip()
        real = real if os.path.isabs(real) else os.path.join(repo, real)
        if os.path.exists(real):
            shutil.copy2(real, idx)             # keep its mtime: git re-hashes entries as new as the index ("racy git")
        env = {"GIT_INDEX_FILE": idx}
        # untracked nested repos list as "dir/"; `add -A` would make them gitlinks, or abort on one with no commit
        nested = [f":(exclude,literal){p}" for p in git(repo, "ls-files", "-o", "--exclude-standard", "-z").split("\0")
                  if p.endswith("/")]
        git(repo, "-c", "core.safecrlf=false", "add", "-A", "--", *nested, env=env)  # a safecrlf abort would skip the review
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


def refs(repo):
    """{refname: sha} for every ref, plus HEAD when detached."""
    out = {}
    for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
        name, sha = line.rsplit(" ", 1)
        out[name] = sha
    if git(repo, "symbolic-ref", "-q", "HEAD", check=False) == "":
        head = git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).strip()
        if head:
            out["HEAD"] = head
    return out


def _log(repo, *rev_args, limit=10):
    return git(repo, "log", "--oneline", "--no-decorate", f"-{limit}", *rev_args, check=False).strip().splitlines()


def ref_activity(repo, old, new):
    """Readable lines describing ref moves between two `refs()` maps: commits, rewrites, pushes, deletions."""
    lines = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        if a is None:
            lines.append(f"created {name} at {b[:7]}")
        elif b is None:
            lines.append(f"deleted {name} (was {a[:7]})")
        elif subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b],
                            capture_output=True).returncode == 0:
            commits = _log(repo, f"{a}..{b}")
            n = git(repo, "rev-list", "--count", f"{a}..{b}").strip()
            lines.append(f"{name}: {a[:7]} -> {b[:7]}, {n} new commit(s)"
                         + "".join(f"\n    {c}" for c in commits))
        else:
            commits = _log(repo, b, "--not", a)
            lines.append(f"{name}: REWRITTEN {a[:7]} -> {b[:7]} (not a fast-forward; old history replaced)"
                         + "".join(f"\n    {c}" for c in commits))
    return lines
