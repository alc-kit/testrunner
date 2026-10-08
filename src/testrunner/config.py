"""Configuration files: YAML or TOML, chosen by extension.

Two kinds of file:
  the runner config  (testrunner.yml / testrunner.toml at the subscriber root) — what the
                     subscriber exports and where its state lives;
  run configs        (configs/<name>.yml|.toml) — one per kind of run, with `extends:`
                     and a `plan:`. Everything else in them is the subscriber's own.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .nolog import NoLog, unwrap

try:
    import yaml
except ImportError:  # TOML-only subscribers need no third-party package at all
    yaml = None

if yaml is not None:
    class _Loader(yaml.SafeLoader):
        """SafeLoader + the `!NOLOG value` shorthand (see nolog.py)."""

    def _nolog(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return NoLog(loader.construct_scalar(node))
        if isinstance(node, yaml.SequenceNode):
            return NoLog(loader.construct_sequence(node, deep=True))
        return NoLog(loader.construct_mapping(node, deep=True))

    _Loader.add_constructor("!NOLOG", _nolog)

EXTENSIONS = (".yml", ".yaml", ".toml")
RUNNER_FILES = ("testrunner.yml", "testrunner.yaml", "testrunner.toml")


class ConfigError(Exception):
    pass


def load_file(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        if path.suffix == ".toml":
            with path.open("rb") as f:
                data = tomllib.load(f)
        elif path.suffix in (".yml", ".yaml"):
            if yaml is None:
                raise ConfigError(f"{path}: YAML needs PyYAML (python3-pyyaml); or use TOML")
            with path.open() as f:
                data = yaml.load(f, Loader=_Loader)
        else:
            raise ConfigError(f"{path}: unknown config format (want {', '.join(EXTENSIONS)})")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    except Exception as e:  # yaml.YAMLError without importing yaml at module level
        if yaml is not None and isinstance(e, yaml.YAMLError):
            raise ConfigError(f"{path}: {e}") from e
        raise
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    return data


def deep_merge(base: dict, over: dict) -> dict:
    """`over` wins; mappings merge key by key; anything else (lists too) is replaced."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def lookup(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_dotted(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
        if not isinstance(cur, dict):
            raise ConfigError(f"cannot set {dotted}: {part} is not a mapping")
    cur[parts[-1]] = value


@dataclass
class RunnerConfig:
    root: Path
    file: Path
    modules: list[Path] = field(default_factory=list)
    pythonpath: list[Path] = field(default_factory=list)
    shell_actions: dict[str, dict] = field(default_factory=dict)
    state_dir: Path = Path("state")
    states: dict[str, dict] = field(default_factory=dict)
    paths: dict[str, list] = field(default_factory=dict)
    default_path: str | None = None
    configs_dir: Path = Path("configs")
    config_env: str | None = None
    config_link: str | None = None
    default_config: str = "default"
    params_section: str = "params"
    input: dict[str, Any] = field(default_factory=dict)
    release_when: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def find_runner_config(start: Path) -> Path:
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        for name in RUNNER_FILES:
            if (d / name).is_file():
                return d / name
    raise ConfigError(f"no {' / '.join(RUNNER_FILES)} in {cur} or above")


KNOWN_RUNNER_KEYS = {"modules", "actions", "state_dir", "states", "paths", "default_path",
                     "configs", "params_section", "input", "scenario", "pythonpath"}


def load_runner_config(path: Path) -> RunnerConfig:
    path = Path(path).resolve()
    data = load_file(path)
    # `x-*` keys are free for YAML anchors (the docker-compose convention)
    unknown = {k for k in data if not str(k).startswith("x-")} - KNOWN_RUNNER_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}")
    root = path.parent
    states = {}
    for var, spec in (data.get("states") or {}).items():
        if isinstance(spec, list):
            spec = {"values": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("values"), list) or not spec["values"]:
            raise ConfigError(f"{path}: states.{var} needs a non-empty `values` list")
        if "initial" in spec and spec["initial"] not in spec["values"]:
            raise ConfigError(f"{path}: states.{var}.initial {spec['initial']!r} is not one of its values")
        states[var] = spec
    paths = data.get("paths") or {}
    for name, steps in paths.items():
        if not isinstance(steps, list):
            raise ConfigError(f"{path}: paths.{name} must be a list of steps")
    default_path = data.get("default_path")
    if default_path is None and len(paths) == 1:
        default_path = next(iter(paths))
    if default_path is not None and default_path not in paths:
        raise ConfigError(f"{path}: default_path {default_path!r} is not in paths")
    configs = data.get("configs") or {}
    scenario = data.get("scenario") or {}
    if set(scenario) - {"release_when"}:
        raise ConfigError(f"{path}: scenario takes only release_when")
    release_when = scenario.get("release_when") or {}
    for var, val in release_when.items():
        if var not in states or val not in states[var]["values"]:
            raise ConfigError(f"{path}: scenario.release_when {var}={val!r} is not a declared state value")
    return RunnerConfig(
        root=root, file=path,
        modules=[root / m for m in (data.get("modules") or [])],
        pythonpath=[root / p for p in (data.get("pythonpath") or [])],
        shell_actions=data.get("actions") or {},
        state_dir=root / data.get("state_dir", "state"),
        states=states, paths=paths, default_path=default_path,
        configs_dir=root / configs.get("dir", "configs"),
        config_env=configs.get("env"), config_link=configs.get("link"),
        default_config=configs.get("default", "default"),
        params_section=data.get("params_section", "params"),
        input=data.get("input") or {},
        release_when=release_when,
        raw=data,
    )


@dataclass
class RunConfig:
    file: Path | None
    how: str
    data: dict[str, Any]
    nolog_paths: list[str] = field(default_factory=list)   # dotted paths marked NOLOG
    # set when this run JOINED a scenario: its description. `how` then stays the way the
    # scenario's config was ORIGINALLY selected (--config, $ENV, ./link, default), so a
    # subscriber can still tell a deliberately chosen config from the default one.
    scenario: str = ""

    @property
    def plan(self) -> Any:
        return self.data.get("plan")


def resolve_config_file(rc: RunnerConfig, name: str | None) -> tuple[Path | None, str]:
    """--config NAME|PATH > <env> > ./<link> > configs/<default>; None when none exists."""
    def by_name(n: str) -> Path:
        p = Path(n)
        if p.suffix in EXTENSIONS and p.exists():
            return p.resolve()
        for base in (rc.configs_dir, rc.configs_dir / "local"):
            for ext in EXTENSIONS:
                cand = base / f"{n}{ext}"
                if cand.is_file():
                    return cand
        raise ConfigError(f"no run config {n!r} in {rc.configs_dir} (or local/), and no such file")

    if name:
        return by_name(name), "--config"
    if rc.config_env and os.environ.get(rc.config_env):
        return by_name(os.environ[rc.config_env]), f"${rc.config_env}"
    if rc.config_link and (rc.root / rc.config_link).exists():
        return (rc.root / rc.config_link).resolve(), f"./{rc.config_link}"
    for ext in EXTENSIONS:
        cand = rc.configs_dir / f"{rc.default_config}{ext}"
        if cand.is_file():
            return cand, "default"
    return None, "none"


def load_run_config(rc: RunnerConfig, name: str | None = None) -> RunConfig:
    file, how = resolve_config_file(rc, name)
    if file is None:
        return RunConfig(None, how, {})
    seen: list[Path] = []

    def load(p: Path) -> dict:
        p = p.resolve()
        if p in seen:
            chain = " -> ".join(str(s.name) for s in [*seen, p])
            raise ConfigError(f"extends loop: {chain}")
        seen.append(p)
        data = load_file(p)
        parent = data.pop("extends", None)
        if parent is None:
            return data
        base_dir = p.parent
        cand = None
        for d in (base_dir, rc.configs_dir):
            for ext in ("", *EXTENSIONS):
                c = d / f"{parent}{ext}"
                if c.is_file():
                    cand = c
                    break
            if cand:
                break
        if cand is None:
            raise ConfigError(f"{p}: extends {parent!r}, which does not exist")
        base = load(cand)
        merged = deep_merge(base, data)
        # A plan is a whole: a child that states one replaces the parent's.
        if "plan" in data:
            merged["plan"] = copy.deepcopy(data["plan"])
        return merged

    data = load(file)
    plan = data.get("plan")
    if isinstance(plan, dict) and True in plan:
        # YAML 1.1 reads a bare `on:` key as the boolean true (as in GitHub Actions files)
        data["plan"] = {("on" if k is True else k): v for k, v in plan.items()}
    data, nolog_paths = unwrap(data)
    return RunConfig(file, how, data, nolog_paths)


def parse_with(items: list[str]) -> dict[str, Any]:
    """--with key=value (repeatable). Values are parsed as YAML scalars when YAML exists."""
    out: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--with {item!r}: want key=value")
        k, v = item.split("=", 1)
        if yaml is not None:
            try:
                v = yaml.safe_load(v)
            except yaml.YAMLError:
                pass
        out[k.strip()] = v
    return out
