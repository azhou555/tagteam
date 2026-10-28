"""Config: bundled defaults < ~/.tagteam/config.toml < <repo>/.tagteam.toml (repo may only add scrutiny: REPO_RULES).

Old-shape files keep loading through a shim that maps their keys. `install`, `doctor` and `config` log the old
keys once per process (`note_legacy`); hooks never do.
"""
import copy
import json
import math
import pathlib
import re
import tomllib

from . import resources, state
from .verdict import RANK

PER_CHANGE_KINDS = ("small", "large", "high_risk", "docs")
LEGACY_ROUTING = {"small": "per_change", "large": "per_change", "high_risk": "per_change", "docs": "per_change"}
STRING_LISTS = (("reviewers", "use"), ("authors", "review"), ("triggers", "kinds"))
LEGACY_QUOTA = ("primary_use", "tier_penalty", "unknown_headroom")


class ConfigError(Exception):
    pass


_legacy_noted = False


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _set(d, dotted, value):
    *parents, leaf = dotted.split(".")
    for p in parents:
        d = d.setdefault(p, {})
    d[leaf] = value


def _shim(raw):
    """Old-shape keys -> new shape. Returns (new_config, list of legacy keys found). New-shape keys win."""
    raw = copy.deepcopy(raw)
    out, found = {}, []

    def take(table, key, dest):
        t = raw.get(table)
        if isinstance(t, dict) and key in t:
            found.append(f"{table}.{key}")
            _set(out, dest, t.pop(key))

    take("review", "max_rounds", "limits.max_rounds_per_turn")
    take("review", "min_changed_lines", "triggers.min_changed_lines")
    for k in LEGACY_ROUTING:
        take("routing", k, f"review.per_change.{k}")
    take("routing", "large_lines", "review.large_lines")
    take("routing", "cooldown_min", "limits.cooldown_min")
    for k in LEGACY_QUOTA:
        take("routing", k, f"routing.quota.{k}")
    agents = raw.pop("agents", None)
    if isinstance(agents, dict):
        for name, a in agents.items():
            for k, v in a.items():
                found.append(f"agents.{name}.{k}")
                dest = "routing.quota.agents" if k in ("tier", "weight", "quota") else "reviewers"
                _set(out, f"{dest}.{name}.{k}", v)
    for t in ("review", "routing"):
        if raw.get(t) == {}:
            del raw[t]
    return _merge(out, raw), found


def _kind(v):
    return "table" if isinstance(v, dict) else "list" if isinstance(v, list) else \
        "bool" if isinstance(v, bool) else "number" if isinstance(v, (int, float)) else "str"


def _mismatch(defaults, new, prefix=""):
    """First dotted key whose type differs from the default's, or None. Keys with no default are free-form."""
    for k, v in new.items():
        if k in defaults:
            if _kind(v) != _kind(defaults[k]):
                return prefix + k
            if isinstance(v, dict):
                bad = _mismatch(defaults[k], v, f"{prefix}{k}.")
                if bad:
                    return bad
    return None


def _bad_list(cfg):
    """Dotted key of a string-list setting that isn't a list of strings, or None. `reviewers.use` has no default."""
    for t, k in STRING_LISTS:
        v = cfg.get(t, {}).get(k) if isinstance(cfg.get(t), dict) else None
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return f"{t}.{k}"
    return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _lower(floor=None):
    def rule(v, cur):
        v = max(v, floor) if _num(v) and floor is not None else v
        return v if _num(v) and v < cur else None
    return rule


def _raise(v, cur):
    return v if _num(v) and v > cur else None


def _add(v, cur):
    added = [x for x in v if isinstance(x, str) and x not in cur] if isinstance(v, list) else []
    return cur + added if added else None


def _enable(v, cur):
    return True if v is True and cur is False else None


def _stricter(v, cur):
    return v if v in RANK and cur in RANK and RANK[v] > RANK[cur] else None


# The only keys a repo file may set, each with the one direction that adds scrutiny. Everything else is dropped.
REPO_RULES = {
    "authors.review": _add, "triggers.kinds": _add, "classify.risk": _add,
    "triggers.min_changed_lines": _lower(), "review.min_confidence": _lower(),
    "review.block_at": _stricter,
    "review.large_lines": _lower(1),
    "limits.max_rounds_per_turn": _lower(1), "limits.max_reviews_per_hour": _lower(1),
    **{f"review.per_change.{k}": _raise for k in PER_CHANGE_KINDS},
    **{f"review.blocking.{k}": _enable for k in PER_CHANGE_KINDS},
}


def _conservative(cfg, over):
    """A repo file is untrusted (the author can edit it mid-turn), so it may only add scrutiny.
    Returns (the settings that do, dotted keys dropped)."""
    kept, dropped, cur = {}, [], flat(cfg)
    for key, v in flat(over).items():
        rule = REPO_RULES.get(key)
        new = rule(v, cur[key]) if rule and key in cur else None
        if new is None:
            dropped.append(key)
        else:
            _set(kept, key, new)
    return kept, dropped


def _non_finite(node, path=""):
    """Dotted key of the first nan or inf in a parsed TOML tree, or None. They'd slip past every limit comparison."""
    if isinstance(node, float) and not math.isfinite(node):
        return path
    for k, v in (node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()):
        bad = _non_finite(v, f"{path}.{k}" if path else str(k))
        if bad:
            return bad
    return None


def _read(p, label, defaults):
    try:
        raw = tomllib.loads(p.read_text())
        new, legacy = _shim(raw)
        bad = _mismatch(defaults, new) or _bad_list(new)
        if bad:
            raise ValueError(f"{bad} has the wrong type")
        bad = _non_finite(new)
        if bad:
            raise ValueError(f"{bad} must be a finite number")
    except (OSError, ValueError, AttributeError, TypeError) as e:
        raise ConfigError(f"{p}: {e}") from e
    return new, [{"file": str(p), "source": label, "keys": legacy}] if legacy else []


def load_with_sources(repo=None, note_legacy=False):
    global _legacy_noted
    cfg = tomllib.loads(resources.text("config.default.toml"))
    sources = dict.fromkeys(flat(cfg), "default")
    legacy = []
    user = state.HOME / "config.toml"
    if user.is_file():
        over, found = _read(user, "user", cfg)
        legacy += found
        _merge(cfg, over)
        sources.update(dict.fromkeys(flat(over), "user"))
    rf = repo and (pathlib.Path(repo) / ".tagteam.toml")
    if rf and rf.is_file():
        over, found = _read(rf, "repo", cfg)
        legacy += found
        over, dropped = _conservative(cfg, over)
        if dropped:
            state.log("repo config ignored", file=str(rf), keys=sorted(dropped))
        _merge(cfg, over)
        sources.update(dict.fromkeys(flat(over), "repo"))
    if note_legacy and legacy and not _legacy_noted:
        _legacy_noted = True
        state.log("deprecated config keys", files=legacy)
    return cfg, sources


def load(repo=None, note_legacy=False):
    return load_with_sources(repo, note_legacy)[0]


def save_user(table, key, items):
    """Set `key = [..strings..]` under `[table]` in ~/.tagteam/config.toml, keeping every other line as it is."""
    p = state.HOME / "config.toml"
    text = p.read_text() if p.is_file() else ""
    line = f"{key} = {json.dumps(list(items))}"
    head = re.search(rf"^\[{re.escape(table)}\][ \t]*(#.*)?$", text, re.M)
    if not head:
        new = text + ("\n" if text and not text.endswith("\n\n") else "") + f"[{table}]\n{line}\n"
    else:
        end = re.search(r"^\[", text[head.end():], re.M)
        stop = head.end() + end.start() if end else len(text)
        body = text[head.end():stop]
        old = re.search(rf"^{re.escape(key)}[ \t]*=[ \t]*\[.*?\][ \t]*(#.*)?$", body, re.M | re.S)
        body = body[:old.start()] + line + body[old.end():] if old else "\n" + line + body
        new = text[:head.end()] + body + text[stop:]
    try:
        tomllib.loads(new)
    except ValueError as e:
        raise ConfigError(f"{p}: can't update {table}.{key} automatically ({e}); edit it by hand") from e
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new)


def migrate_routing(dry_run=False):
    """An existing pre-packaging user config (no routing.mode, and no authors/reviewers saved by a new install)
    keeps routing by quota: write `mode = "quota"`. The agent table comes from the bundled defaults and the
    user's own [agents.*] keys. Returns True if it wrote (or would write) the line. A fresh install stays ordered."""
    p = state.HOME / "config.toml"
    try:
        raw = tomllib.loads(p.read_text()) if p.is_file() else None
    except (OSError, ValueError):
        return False
    if raw is None or "mode" in (raw.get("routing") or {}) or "use" in (raw.get("reviewers") or {}) \
            or "review" in (raw.get("authors") or {}):
        return False
    text = p.read_text()
    head = re.search(r"^\[routing\][ \t]*(#.*)?$", text, re.M)
    new = (text[:head.end()] + '\nmode = "quota"' + text[head.end():] if head
           else text + ("" if not text or text.endswith("\n") else "\n") + '\n[routing]\nmode = "quota"\n')
    try:
        tomllib.loads(new)
    except ValueError:
        return False
    if not dry_run:
        p.write_text(new)
    return True
