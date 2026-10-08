"""The scenario: the run config a directory is committed to.

The first state-changing run in a directory (or `--select`) SELECTS the scenario: the
fully resolved run config is snapshotted into the store. From then on every runner in
that directory — later, or in parallel — runs on that snapshot:
  * no --config / env selection            -> adheres silently
  * an explicit selection of the same file -> adheres
  * an explicit selection of another one   -> REFUSED, naming the scenario
  * the config file edited since           -> warned; the snapshot still applies
`--with` stays per invocation: it sets parameters for this run's steps and is not
recorded. The scenario ends with `--release`, or when a state-changing run leaves the
state the runner config names in `scenario.release_when` (e.g. the system torn down).
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import ConfigError, RunConfig, RunnerConfig, load_run_config, resolve_config_file
from .store import Store, utcnow


class ScenarioError(ConfigError):
    pass


@dataclass
class Selection:
    run_config: RunConfig
    scenario: dict | None          # the record in force (None: no scenario, nothing claimed)
    claimed: bool = False          # this invocation selected it
    notes: list[str] = field(default_factory=list)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def describe(rec: dict) -> str:
    name = Path(rec["config_file"]).name if rec.get("config_file") else "(no run config)"
    return f"{name}, selected {rec.get('selected_at', '?')} by run {rec.get('run_id', '?')}"


def explicit_choice(rc: RunnerConfig, name: str | None) -> str | None:
    if name:
        return name
    if rc.config_env and os.environ.get(rc.config_env):
        return os.environ[rc.config_env]
    return None


def adhere(rc: RunnerConfig, rec: dict, choice: str | None) -> Selection:
    file = Path(rec["config_file"]) if rec.get("config_file") else None
    if choice is not None:
        chosen, how = resolve_config_file(rc, choice)
        if chosen is None or file is None or chosen.resolve() != file.resolve():
            raise ScenarioError(
                f"this directory is committed to scenario {describe(rec)}; {how} asks for "
                f"{chosen.name if chosen else choice!r}. Run without a config selection to join the "
                f"scenario, or end it first with --release")
    sel = Selection(RunConfig(file, f"scenario: {describe(rec)}", rec["data"],
                              rec.get("nolog_paths", [])), rec)
    if file is not None and file.exists():
        try:
            fresh = load_run_config(rc, str(file)).data
        except ConfigError:
            fresh = None
        if fresh != rec["data"]:
            sel.notes.append(f"{file.name} changed since the scenario was selected; "
                             f"the scenario's snapshot applies (--release to pick up the change)")
    return sel


def select(rc: RunnerConfig, store: Store, name: str | None, claim: bool, run_id: str) -> Selection:
    choice = explicit_choice(rc, name)
    rec = store.get_scenario()
    if rec is not None:
        return adhere(rc, rec, choice)
    run_config = load_run_config(rc, name)
    if not claim:
        return Selection(run_config, None)
    record = {"config_file": str(run_config.file) if run_config.file else None,
              "how": run_config.how, "data": run_config.data,
              # the snapshot holds the real values (later runners need them, as the config
              # file on disk does); which ones are NOLOG travels with it
              "nolog_paths": run_config.nolog_paths,
              "selected_at": utcnow(), "run_id": run_id, "pid": os.getpid()}
    claimed, rec = store.claim_scenario(record)
    if not claimed:      # a parallel runner won the race: join ITS scenario
        return adhere(rc, rec, choice)
    store.journal.append("scenario_selected", run=run_id, config=record["config_file"])
    return Selection(run_config, rec, claimed=True)
