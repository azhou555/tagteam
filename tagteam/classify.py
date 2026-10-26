"""Artifact kind (code | design | plan) and risk, from path globs."""
from .changes import ignored


def kind(path, cfg):
    c = cfg["classify"]
    if ignored(path, c["plan"]):
        return "plan"
    if ignored(path, c["design"]):
        return "design"
    return "code"


def annotate(artifacts, cfg):
    for a in artifacts:
        a.setdefault("kind", kind(a["path"], cfg))
        a["risk"] = "high" if ignored(a["path"], cfg["classify"]["risk"]) else "normal"
    return artifacts
