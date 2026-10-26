"""Config: bundled defaults < ~/.tagteam/config.toml < <repo>/.tagteam.toml (repo limited to REPO_ALLOWED).

Old-shape files keep loading through a shim that maps their keys and logs one deprecation line.
"""
import copy
import pathlib
import tomllib

from . import resources, state

REPO_ALLOWED = {"triggers", "authors", "limits", "review", "classify"}
LEGACY_ROUTING = {"small": "per_change", "large": "per_change", "high_risk": "per_change", "docs": "per_change"}
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


def _conservative(cfg, over):
    """A repo file may only tighten: lower limits and diff size, add ignore patterns. Drops no-op keys."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    for t, k in [("limits", k) for k in over.get("limits", {})] + [("review", "max_diff_chars")]:
        if k in over.get(t, {}) and num(over[t][k]) and num(cfg.get(t, {}).get(k)):
            if over[t][k] >= cfg[t][k]:
                del over[t][k]
    rv = over.get("review", {})
    if "ignore" in rv:
        cur = cfg["review"]["ignore"]
        added = [p for p in rv["ignore"] if p not in cur]
        if added:
            rv["ignore"] = cur + added
        else:
            del rv["ignore"]
    return over


def _read(p, label, defaults):
    try:
        raw = tomllib.loads(p.read_text())
        new, legacy = _shim(raw)
        bad = _mismatch(defaults, new)
        if bad:
            raise ValueError(f"{bad} has the wrong type")
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
