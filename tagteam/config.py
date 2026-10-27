"""Config: bundled defaults < ~/.tagteam/config.toml < <repo>/.tagteam.toml (repo limited to REPO_ALLOWED).

Old-shape files keep loading through a shim that maps their keys and logs one deprecation line.
"""
import copy
import json
import math
import pathlib
import re
import tomllib

from . import resources, state

REPO_ALLOWED = {"triggers", "authors", "limits", "review", "classify"}
LEGACY_ROUTING = {"small": "per_change", "large": "per_change", "high_risk": "per_change", "docs": "per_change"}
STRING_LISTS = (("reviewers", "use"), ("authors", "review"), ("triggers", "kinds"))
LEGACY_QUOTA = ("primary_use", "tier_penalty", "unknown_headroom")


class ConfigError(Exception):
    pass


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


def _conservative(cfg, over):
    """A repo file may only tighten: lower limits and diff size, longer cooldowns, more ignore patterns, more authors and
    trigger kinds, lower thresholds, a stricter block_at, more reviewers per change. Drops every other key."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    for t, k in [("limits", k) for k in over.get("limits", {})] + [("review", "max_diff_chars")]:
        if k in over.get(t, {}) and num(over[t][k]) and num(cfg.get(t, {}).get(k)):
            # a longer cooldown is the safer one; every other limit is safer lower
            looser = over[t][k] <= cfg[t][k] if k == "cooldown_min" else over[t][k] >= cfg[t][k]
            if looser:
                del over[t][k]
    for t, k in (("authors", "review"), ("triggers", "kinds")):          # a repo can add, never remove
        if isinstance(over.get(t), dict) and isinstance(over[t].get(k), list):
            added = [x for x in over[t][k] if x not in cfg[t][k]]
            if added:
                over[t][k] = cfg[t][k] + added
            else:
                del over[t][k]
    for t, k in (("triggers", "min_changed_lines"), ("review", "min_confidence")):   # lower reviews more
        if k in over.get(t, {}) and num(over[t][k]) and over[t][k] >= cfg[t][k]:
            del over[t][k]
    if over.get("review", {}).get("block_at") is not None:               # blocking on more severities is stricter
        order = ["blocker", "major", "minor"]
        v, cur = over["review"]["block_at"], cfg["review"]["block_at"]
        if v not in order or cur not in order or order.index(v) <= order.index(cur):
            del over["review"]["block_at"]
    pc = over.get("review", {}).get("per_change")
    if isinstance(pc, dict):                                             # never fewer reviewers than the user chose
        for k in [k for k, v in pc.items() if not num(v) or v <= cfg["review"]["per_change"].get(k, 0)]:
            del pc[k]
    rv = over.get("review", {})
    if "ignore" in rv:
        cur = cfg["review"]["ignore"]
        added = [p for p in rv["ignore"] if p not in cur]
        if added:
            rv["ignore"] = cur + added
        else:
            del rv["ignore"]
    return over


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
    if legacy:
        state.log("deprecated config keys", file=str(p), source=label, keys=legacy)
    return new


def load_with_sources(repo=None):
    cfg = tomllib.loads(resources.text("config.default.toml"))
    sources = dict.fromkeys(flat(cfg), "default")
    user = state.HOME / "config.toml"
    if user.is_file():
        over = _read(user, "user", cfg)
        _merge(cfg, over)
        sources.update(dict.fromkeys(flat(over), "user"))
    rf = repo and (pathlib.Path(repo) / ".tagteam.toml")
    if rf and rf.is_file():
        over = _read(rf, "repo", cfg)
        dropped = {k: v for k, v in over.items() if k not in REPO_ALLOWED}
        if dropped:
            state.log("repo config ignored", file=str(rf), keys=sorted(flat(dropped)))
        over = _conservative(cfg, {k: v for k, v in over.items() if k in REPO_ALLOWED})
        _merge(cfg, over)
        sources.update(dict.fromkeys(flat(over), "repo"))
    return cfg, sources


def load(repo=None):
    return load_with_sources(repo)[0]


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
