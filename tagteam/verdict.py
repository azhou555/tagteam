"""Merge reviewer findings into one allow/block decision and the message the author sees."""
import difflib

from .adapters import MARKER

RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}


def same(a, b):
    """The one test for "the same issue": same file, nearby line, similar claim."""
    return (a["file"] == b["file"] and abs((a["line"] or 0) - (b["line"] or 0)) <= 3
            and difflib.SequenceMatcher(None, a["claim"].lower(), b["claim"].lower()).ratio() > 0.55)


def dedupe(findings):
    """Same issue -> keep the most severe; remember who agreed."""
    kept = []
    for f in sorted(findings, key=lambda f: (RANK[f["severity"]], -f["confidence"])):
        dup = next((k for k in kept if same(k, f)), None)
        if dup:
            dup["reviewers"] = sorted(set(dup["reviewers"]) | {f["reviewer"]})
            dup["confidence"] = max(dup["confidence"], f["confidence"])
        else:
            kept.append({**f, "reviewers": [f["reviewer"]]})
    return kept


def gated(issues, gate="block"):
    """Issues opened before gates existed block."""
    return [f for f in issues if f.get("gate", "block") == gate]


def decide(results, cfg, round_no, mode="block", carried=()):
    """results: {reviewer: result-or-None}. Returns (action, issues, advisory): issues are the findings at or
    above `block_at`, each with a `gate`, and only `block`-gated ones block. A blocker always blocks, a
    re-checked suggestion stays one, and anything else takes the review's `mode`."""
    rv = cfg["review"]
    findings = [{**f, "reviewer": r} for r, res in results.items() if res for f in res["findings"]]
    merged = dedupe(findings)
    advise = gated(carried, "advise")
    for f in merged:
        f["gate"] = "block" if f["severity"] == "blocker" else "advise" if any(same(f, c) for c in advise) else mode
    threshold = RANK[rv["block_at"]]
    issues = [f for f in merged if RANK[f["severity"]] <= threshold and f["confidence"] >= rv["min_confidence"]]
    advisory = [f for f in merged if f not in issues]
    action = "block" if gated(issues) and round_no <= cfg["limits"]["max_rounds_per_turn"] else "allow"
    return action, issues, advisory


def _loc(f):
    return f"{f['file']}:{f['line']}" if f.get("line") else (f["file"] or "(general)")


def items(issues):
    lines = []
    for i, f in enumerate(issues, 1):
        lines.append(f"{i}. [{f['severity']}] {_loc(f)}: {f['claim']}  ({', '.join(f['reviewers'])})")
        if f["evidence"]:
            lines.append(f"   Evidence: {f['evidence']}")
        if f["suggested_fix"]:
            lines.append(f"   Fix: {f['suggested_fix']}")
    return lines


def message(assignments, issues, advisory, round_no, max_rounds, failed):
    who = ", ".join(f"{r} ({'/'.join(t['lens'] for t in ts)})" for r, ts in assignments.items())
    lines = [f"{MARKER} Cross-review by {who}, round {round_no}/{max_rounds}."]
    if gated(issues):
        lines.append("Blocking: fix each item below. If you disagree with one, say why in your reply; "
                     "reviewers see your reply next round.\n")
        lines += items(gated(issues))
    if gated(issues, "advise"):
        lines.append("\nSuggestions (not blocking; fix when convenient):")
        lines += items(gated(issues, "advise"))
    if advisory:
        lines.append("\nMinor notes (optional):")
        lines += [f"- [{f['severity']}] {_loc(f)}: {f['claim']}" for f in advisory[:8]]
    if failed:
        lines.append(f"\n(Reviewer(s) unavailable this round: {', '.join(failed)})")
    return "\n".join(lines)
