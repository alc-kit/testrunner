"""Configuration files: YAML or TOML, chosen by extension.

Two kinds of file:
  the runner config  (testrunner.yml / testrunner.toml at the subscriber root) — what the
                     subscriber exports and where its state lives;
  run configs        (configs/<name>.yml|.toml) — one per kind of run, with `extends:`
                     and a `plan:`. Everything else in them is the subscriber's own.
"""
from __future__ import annotations

import copy
import logging
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

log = logging.getLogger(__name__)

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
    # files holding more `actions:` (walk actions): globs, relative to the root
    action_files: list[str] = field(default_factory=list)
    state_dir: Path = Path("state")
    states: dict[str, dict] = field(default_factory=dict)
    paths: dict[str, list] = field(default_factory=dict)
    default_path: str | None = None
    # what the CLI word `all` runs when the run config's plan is empty (`all: {fallback: <path>}`)
    all_fallback: str | None = None
    # extensions' own settings, by extension name (`ext: {rig: {...}}`): each extension
    # validates its part
    ext: dict[str, dict] = field(default_factory=dict)
    configs_dir: Path = Path("configs")
    config_env: str | None = None
    config_link: str | None = None
    default_config: str = "default"
    params_section: str = "params"
    input: dict[str, Any] = field(default_factory=dict)
    release_when: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    # the subscriber's `run_config` hooks (collect() fills it): they see every run config
    # as it loads, before anything is planned from it
    config_hooks: list = field(default_factory=list)


def _strings(value: Any, path: Path, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: {key}: want a list of paths (globs allowed)")
    return list(value)


def find_runner_config(start: Path) -> Path:
    cur = Path(start).resolve()
    for d in (cur, *cur.parents):
        for name in RUNNER_FILES:
            if (d / name).is_file():
                return d / name
    raise ConfigError(f"no {' / '.join(RUNNER_FILES)} in {cur} or above")


KNOWN_RUNNER_KEYS = {"modules", "actions", "action_files", "state_dir", "states", "paths", "default_path",
                     "configs", "params_section", "input", "scenario", "pythonpath", "all", "ext"}


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
    if "all" in paths:
        raise ConfigError(f"{path}: a path may not be named `all`: on the command line `all` means the "
                          f"run config's plan. Rename it (e.g. `full`) and name it as `all: {{fallback: full}}`")
    ext = data.get("ext") or {}
    if not isinstance(ext, dict) or not all(isinstance(v, dict) for v in ext.values()):
        raise ConfigError(f"{path}: ext: want {{<extension>: {{settings}}}}")
    all_spec = data.get("all") or {}
    if not isinstance(all_spec, dict) or set(all_spec) - {"fallback"}:
        raise ConfigError(f"{path}: all takes only fallback (all: {{fallback: <path>}})")
    all_fallback = all_spec.get("fallback")
    if all_fallback is not None and all_fallback not in paths:
        raise ConfigError(f"{path}: all.fallback {all_fallback!r} is not in paths")
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
        action_files=_strings(data.get("action_files"), path, "action_files"),
        state_dir=root / data.get("state_dir", "state"),
        states=states, paths=paths, default_path=default_path, all_fallback=all_fallback, ext=ext,
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
    # the file that STATES the plan: an ancestor when the plan is inherited through `extends:`
    plan_file: Path | None = None
    # the walk actions this config defines (`actions:`, inherited through `extends:`, imported
    # with `uses:`): name -> (spec, where); and what overrode what, for --plan and the banner
    actions: dict[str, tuple[dict, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def walk_actions(self) -> dict[str, tuple[dict, str]]:
        """The config's walk actions — from the files, or (a joined scenario's snapshot) from
        the data the scenario recorded."""
        if self.actions:
            return self.actions
        where = self.file.name if self.file else "scenario snapshot"
        return {n: (s, where) for n, s in (self.data.get("actions") or {}).items()}

    @property
    def plan(self) -> Any:
        return self.data.get("plan")

    @property
    def plan_of(self) -> str:
        """Where the plan comes from, for people: `sdn.yml (via sdn+lvmthin.yml)`."""
        own = self.file.name if self.file else "(no run config)"
        if self.file is None or self.plan_file is None or self.plan_file.resolve() == self.file.resolve():
            return own
        return f"{self.plan_file.name} (via {own})"


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
        log.debug("run config: --config %r given", name)
        return by_name(name), "--config"
    if rc.config_env and os.environ.get(rc.config_env):
        log.debug("run config: no --config; $%s=%r", rc.config_env, os.environ[rc.config_env])
        return by_name(os.environ[rc.config_env]), f"${rc.config_env}"
    if rc.config_link and (rc.root / rc.config_link).exists():
        log.debug("run config: no --config%s; link %s -> %s", f", ${rc.config_env} unset" if rc.config_env else "",
                  rc.root / rc.config_link, (rc.root / rc.config_link).resolve())
        return (rc.root / rc.config_link).resolve(), f"./{rc.config_link}"
    for ext in EXTENSIONS:
        cand = rc.configs_dir / f"{rc.default_config}{ext}"
        if cand.is_file():
            log.debug("run config: no --config%s%s; the default %s", f", ${rc.config_env} unset" if rc.config_env else "",
                      f", no ./{rc.config_link}" if rc.config_link else "", cand)
            return cand, "default"
    log.debug("run config: none (no --config, env, link or %s/%s.*)", rc.configs_dir, rc.default_config)
    return None, "none"


def _find_config(rc: RunnerConfig, near: Path, name: str, key: str) -> Path:
    """`extends:` / `uses:` NAME: beside the file that names it, then in the configs dir."""
    for d in (near.parent, rc.configs_dir):
        for ext in ("", *EXTENSIONS):
            c = d / f"{name}{ext}"
            if c.is_file():
                return c
    raise ConfigError(f"{near}: {key} {name!r}, which does not exist")


def collect_actions(rc: RunnerConfig, file: Path, chain: tuple[Path, ...] = ()) -> tuple[dict[str, tuple[dict, str]], list[str]]:
    """A run config's WALK ACTIONS: its parent's (`extends:`), then each `uses:` config's (in
    order; its actions only — not its plan, rig or edits), then its own `actions:`. A later
    definition of a name replaces an earlier one, and says so in the notes."""
    file = file.resolve()
    if file in chain:
        raise ConfigError("extends/uses loop: " + " -> ".join(p.name for p in (*chain, file)))
    data = load_file(file)
    acts: dict[str, tuple[dict, str]] = {}
    notes: list[str] = []

    def put(more: dict[str, tuple[dict, str]], how: str) -> None:
        for n, (spec, where) in more.items():
            if n in acts and acts[n][1] != where:
                notes.append(f"action {n!r}: {where} ({how}) replaces {acts[n][1]}")
            acts[n] = (spec, where)

    if data.get("extends") is not None:
        a, n = collect_actions(rc, _find_config(rc, file, str(data["extends"]), "extends"), (*chain, file))
        notes += n
        put(a, "inherited")
    uses = data.get("uses") or []
    if not isinstance(uses, list) or not all(isinstance(u, str) and u for u in uses):
        raise ConfigError(f"{file}: uses: want a list of run config names")
    for u in uses:
        a, n = collect_actions(rc, _find_config(rc, file, u, "uses"), (*chain, file))
        notes += n
        put(a, f"uses {u}")
    own = data.get("actions") or {}
    if not isinstance(own, dict):
        raise ConfigError(f"{file}: actions: want a mapping of name -> walk action (steps:)")
    put({n: (spec, file.name) for n, spec in own.items()}, "own")
    return acts, notes


def _edits(path: Path, data: dict) -> list:
    e = data.get("edit") or []
    if not isinstance(e, list):
        raise ConfigError(f"{path}: edit: want a list of edits (repeat/insert/skip/replace)")
    return copy.deepcopy(e)


def load_run_config(rc: RunnerConfig, name: str | None = None) -> RunConfig:
    file, how = resolve_config_file(rc, name)
    if file is None:
        return RunConfig(None, how, apply_config_hooks(rc, {}, how, None, []))
    seen: list[Path] = []
    plan_from: list[Path] = []          # the files that state a plan, ancestors first

    def load(p: Path) -> dict:
        p = p.resolve()
        if p in seen:
            chain = " -> ".join(str(s.name) for s in [*seen, p])
            raise ConfigError(f"extends loop: {chain}")
        seen.append(p)
        data = load_file(p)
        data.pop("uses", None)          # actions only: collected by collect_actions()
        data.pop("actions", None)
        parent = data.pop("extends", None)
        if parent is None:
            if "plan" in data:
                plan_from.append(p)
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
        log.debug("%s extends %s", p.name, cand)
        base = load(cand)
        merged = deep_merge(base, data)
        # Walk edits ACCUMULATE: the parent's first, then this file's (a child edits the
        # inherited plan without restating it)
        if "edit" in data or "edit" in base:
            merged["edit"] = _edits(cand, base) + _edits(p, data)
        # A plan is a whole: a child that states one replaces the parent's.
        if "plan" in data:
            merged["plan"] = copy.deepcopy(data["plan"])
            plan_from.append(p)
        return merged

    data = load(file)
    plan = data.get("plan")
    if isinstance(plan, dict) and True in plan:
        # YAML 1.1 reads a bare `on:` key as the boolean true (as in GitHub Actions files)
        data["plan"] = {("on" if k is True else k): v for k, v in plan.items()}
    data, nolog_paths = unwrap(data)
    log.info("loaded run config %s (%s)%s", file, how,
             f"; NOLOG at {nolog_paths}" if nolog_paths else "")
    plan_file = plan_from[-1] if plan_from else None
    if plan_file is not None and plan_file != file.resolve():
        log.info("its plan is inherited from %s", plan_file)
    actions, notes = collect_actions(rc, file)
    if actions:
        # in the data too: a scenario records the data, and a run that joins it has no files to read
        data["actions"] = {n: copy.deepcopy(spec) for n, (spec, _) in actions.items()}
        log.info("run config actions: %s", {n: w for n, (_, w) in actions.items()})
    for n in notes:
        log.info("run config: %s", n)
    return RunConfig(file, how, apply_config_hooks(rc, data, how, file, nolog_paths), nolog_paths,
                     plan_file=plan_file, actions=actions, notes=notes)


def _leaves(data: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(data, dict) or not data:
        return {prefix: data} if prefix else {}
    out: dict[str, Any] = {}
    for k, v in data.items():
        out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def apply_config_hooks(rc: RunnerConfig, data: dict, how: str, file: Path | None,
                       nolog_paths: list[str]) -> dict:
    """The `run_config` hooks, in place, as the run config loads: whatever plans from this
    config (a step's `when:`, the scenario snapshot) then sees what the hooks made of it —
    e.g. a value the subscriber takes from the environment. Every changed key is logged."""
    from .fixtures import accepted   # here: fixtures -> discovery -> config is a cycle
    for fn in rc.config_hooks:
        before = _leaves(copy.deepcopy(data))
        kwargs = accepted(fn, {"config": data, "how": how, "file": file, "root": rc.root})
        name = getattr(fn, "__qualname__", str(fn))
        try:
            fn(**kwargs)
        except ConfigError:
            raise
        except Exception as e:
            log.debug("run_config hook %s raised %s: %s", name, type(e).__name__, e, exc_info=True)
            detail = getattr(getattr(e, "outcome", None), "detail", "") or str(e)
            raise ConfigError(f"run_config hook {name} refused {file or 'the run config'}: {detail}") from e
        after = _leaves(data)
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k, _NONE) != after.get(k, _NONE))
        for k in changed:
            masked = any(k == p or k.startswith(p + ".") for p in nolog_paths)
            log.info("run_config hook %s: %s %r -> %r", name, k,
                     "<NOLOG>" if masked else before.get(k), "<NOLOG>" if masked else after.get(k))
        if not changed:
            log.debug("run_config hook %s: changed nothing", name)
    return data


_NONE = object()


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
