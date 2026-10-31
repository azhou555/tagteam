import sys, collections
sys.path.insert(0, ".")
from tests.helpers import make_repo
from tagteam import changes, sandbox
errs = collections.Counter()
real = sandbox._worktree
def wrapped(r, c):
    try:
        return real(r, c)
    except Exception as e:
        errs[str(e).strip().splitlines()[-1][:200]] += 1
        raise
sandbox._worktree = wrapped
fails = 0
for i in range(150):
    repo = make_repo(); tree = changes.snapshot(repo)
    with sandbox.checkouts(repo, tree, ["a", "b", "c", "d"], cwd=repo) as m:
        fails += len(set(m.values())) != 4
print("fallbacks:", fails, "/ 150"); [print(n, e) for e, n in errs.items()]
