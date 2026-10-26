"""Pick reviewers: never the author, reviewer-tier agents first, primaries only as overflow.

Pure function of (author, groups, cfg, headroom, load) so it is fully unit-testable.
"""
LENSES = {
    "code": ["correctness", "tests", "design"],
    "design": ["design", "correctness", "tests"],
    "plan": ["design", "correctness", "tests"],
}


def needed(kind, artifacts, rv):
    per = rv["per_change"]
    if kind in ("design", "plan"):
        n = per["docs"]
    elif sum(a["lines_changed"] for a in artifacts) >= rv["large_lines"]:
        n = per["large"]
    else:
        n = per["small"]
    if any(a.get("risk") == "high" for a in artifacts):
        n = max(n, per["high_risk"])
    return n


def eligible(author, cfg, headroom):
    r = cfg["routing"]["quota"]
    out = {}
    for name, a in r["agents"].items():
        if name == author:
            continue
        h = headroom.get(name)
        if h is None and a["tier"] == "primary":    # can't prove a primary is above its reserve floor
            continue
        h = r["unknown_headroom"] if h is None else h
        if h <= 0.05:
            continue
        if a["tier"] == "primary" and (r["primary_use"] == "never" or h < a["quota"].get("reserve_floor", 0)):
            continue
        out[name] = h
    return out


def route(author, groups, cfg, headroom, load):
    """groups: {kind: [artifact]}. Returns {reviewer: [{kind, lens, artifacts}]}."""
    r = cfg["routing"]["quota"]
    agents = r["agents"]
    pool = eligible(author, cfg, headroom)
    load = dict(load)
    plan = {}
    for kind in ("plan", "design", "code"):
        arts = groups.get(kind)
        if not arts:
            continue
        n = min(needed(kind, arts, cfg["review"]), len(pool))

        def score(a):
            s = (load.get(a, 0) + 1) / (agents[a]["weight"][kind] * pool[a])
            return s * (r["tier_penalty"] if agents[a]["tier"] == "primary" else 1.0)

        if r["primary_use"] == "balanced":
            picked = sorted(pool, key=lambda a: (score(a), a))[:n]
        else:
            reviewers = sorted((a for a in pool if agents[a]["tier"] == "reviewer"), key=lambda a: (score(a), a))
            primaries = sorted((a for a in pool if agents[a]["tier"] == "primary"), key=lambda a: (-pool[a], a))
            picked = (reviewers + primaries)[:n]
        for lens, a in zip(LENSES[kind], picked):
            plan.setdefault(a, []).append({"kind": kind, "lens": lens, "artifacts": arts})
            load[a] = load.get(a, 0) + 1
    return plan
