"""Pick reviewers from the user's `reviewers.use`: never the author, never an agent outside the list.

`ordered` mode takes the first N available from the list; `quota` mode scores the listed agents by headroom
(reviewer-tier first, primaries only as overflow). Both go through `candidates`, the one place the list applies.
"""
from . import harnesses, health, limits, quota

LENSES = {
    "code": ["correctness", "tests", "design"],
    "design": ["design", "correctness", "tests"],
    "plan": ["design", "correctness", "tests"],
}


def tier(kind, artifacts, rv):
    """small | large | high_risk | docs: the one lookup for reviewer counts (`per_change`) and `blocking`."""
    if any(a.get("risk") == "high" for a in artifacts):
        return "high_risk"
    if kind in ("design", "plan"):
        return "docs"
    return "large" if sum(a["lines_changed"] for a in artifacts) >= rv["large_lines"] else "small"


def needed(kind, artifacts, rv):
    return rv["per_change"][tier(kind, artifacts, rv)]


def candidates(author, cfg):
    """Listed agents that are installed, enabled and not the author, in list order."""
    have, off = harnesses.detected(), limits.disabled()
    if off is None:                                 # unreadable disabled list: nobody is trusted
        return []
    return [a for a in dict.fromkeys(cfg["reviewers"].get("use") or []) if a != author and a in have and a not in off]


def fallback(author, cfg, taken, headroom):
    """The reviewer to take over a fast-failed one's work: the first listed candidate (in quota mode, the first
    `eligible` one, so primary_use and reserve floors hold) not in `taken` and not cooling, healthy before demoted;
    None when there is none."""
    pool = eligible(author, cfg, headroom) if cfg["routing"]["mode"] == "quota" else candidates(author, cfg)
    return next(iter(sorted((a for a in pool if a not in taken and not quota.cooling(a)), key=health.is_demoted)), None)


def eligible(author, cfg, headroom):
    r = cfg["routing"]["quota"]
    out = {}
    for name in candidates(author, cfg):
        a = r["agents"].get(name)
        if a is None:
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
    """groups: {kind: [artifact]}. Returns ({reviewer: [{kind, lens, artifacts}]}, reason); reason says why
    the plan is empty (None when it isn't)."""
    if not cfg["reviewers"].get("use"):
        return {}, "no reviewers configured"
    plan = (_quota if cfg["routing"]["mode"] == "quota" else _ordered)(author, groups, cfg, headroom, load)
    return plan, None if plan else "no listed reviewer is available"


def _assign(groups, cfg, pick):
    plan = {}
    for kind in ("plan", "design", "code"):
        arts = groups.get(kind)
        if arts:
            for lens, a in zip(LENSES[kind], pick(kind, needed(kind, arts, cfg["review"]))):
                plan.setdefault(a, []).append({"kind": kind, "lens": lens, "artifacts": arts})
    return plan


def _ordered(author, groups, cfg, headroom, load):
    pool = sorted((a for a in candidates(author, cfg) if not quota.cooling(a)), key=health.is_demoted)  # stable
    return _assign(groups, cfg, lambda kind, n: pool[:n])


def _quota(author, groups, cfg, headroom, load):
    r = cfg["routing"]["quota"]
    agents = r["agents"]
    pool = eligible(author, cfg, headroom)
    demoted = {a for a in pool if health.is_demoted(a)}
    most = max((needed(k, arts, cfg["review"]) for k, arts in groups.items() if arts), default=0)
    if len(pool) - len(demoted) >= most:
        pool = {a: h for a, h in pool.items() if a not in demoted}
    load = dict(load)

    def pick(kind, n):
        def score(a):
            s = (load.get(a, 0) + 1) / (agents[a]["weight"][kind] * pool[a])
            if agents[a]["tier"] == "primary":
                s *= r["tier_penalty"]
            return s * r["tier_penalty"] if a in demoted else s

        if r["primary_use"] == "balanced":
            picked = sorted(pool, key=lambda a: (score(a), a))[:n]
        else:
            reviewers = sorted((a for a in pool if agents[a]["tier"] == "reviewer"), key=lambda a: (score(a), a))
            primaries = sorted((a for a in pool if agents[a]["tier"] == "primary"), key=lambda a: (-pool[a], a))
            picked = (reviewers + primaries)[:n]
        for a in picked:
            load[a] = load.get(a, 0) + 1
        return picked
    return _assign(groups, cfg, pick)
