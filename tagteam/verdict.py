"""Merge reviewer findings into one allow/block decision and the message the author sees."""
import difflib

from .adapters import MARKER

RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}


def dedupe(findings):
    """Same file, nearby line, similar claim -> keep the most severe; remember who agreed."""
    kept = []
    for f in sorted(findings, key=lambda f: (RANK[f["severity"]], -f["confidence"])):
        dup = next((k for k in kept if k["file"] == f["file"]
                    and abs((k["line"] or 0) - (f["line"] or 0)) <= 3
                    and difflib.SequenceMatcher(None, k["claim"].lower(), f["claim"].lower()).ratio() > 0.55), None)
        if dup:
            dup["reviewers"] = sorted(set(dup["reviewers"]) | {f["reviewer"]})
            dup["confidence"] = max(dup["confidence"], f["confidence"])
        else:
            kept.append({**f, "reviewers": [f["reviewer"]]})
    return kept


def decide(results, cfg, round_no):
    """results: {reviewer: result-or-None}. Returns (action, blocking, advisory)."""
    rv = cfg["review"]
    findings = [{**f, "reviewer": r} for r, res in results.items() if res for f in res["findings"]]
    merged = dedupe(findings)
    threshold = RANK[rv["block_at"]]
    blocking = [f for f in merged if RANK[f["severity"]] <= threshold and f["confidence"] >= rv["min_confidence"]]
    advisory = [f for f in merged if f not in blocking]
    action = "block" if blocking and round_no <= rv["max_rounds"] else "allow"
    return action, blocking, advisory


def _loc(f):
    return f"{f['file']}:{f['line']}" if f.get("line") else (f["file"] or "(general)")


def message(assignments, blocking, advisory, round_no, max_rounds, failed):
    who = ", ".join(f"{r} ({'/'.join(t['lens'] for t in ts)})" for r, ts in assignments.items())
    lines = [f"{MARKER} Cross-review by {who}, round {round_no}/{max_rounds}."]
    if blocking:
        lines.append("Fix each item below. If you disagree with one, say why in your reply; "
                     "reviewers see your reply next round.\n")
        for i, f in enumerate(blocking, 1):
            lines.append(f"{i}. [{f['severity']}] {_loc(f)}: {f['claim']}  ({', '.join(f['reviewers'])})")
            if f["evidence"]:
                lines.append(f"   Evidence: {f['evidence']}")
            if f["suggested_fix"]:
                lines.append(f"   Fix: {f['suggested_fix']}")
    if advisory:
        lines.append("\nMinor notes (optional):")
        lines += [f"- [{f['severity']}] {_loc(f)}: {f['claim']}" for f in advisory[:8]]
    if failed:
        lines.append(f"\n(Reviewer(s) unavailable this round: {', '.join(failed)})")
    return "\n".join(lines)
