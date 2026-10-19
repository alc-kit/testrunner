"""Run Ansible from an action: playbooks, ad-hoc modules, inventory queries.

    ans = Ansible(proc, cwd=ansible_dir, inventory=inv_dir, env={...})
    await ans.playbook("site.yml", limit=["web-1", "web-2"], extra={"x": 1}, rules=[...])

Two rules this module enforces for every caller, because both have cost real runs:

* NEVER AN EMPTY SCOPE. `--limit ''` (or a group with no hosts) matches nothing, and a
  play that matches nothing is skipped with exit 0 — a broken inventory or a destroyed
  system turns every step into a silent no-op that reports success. A `limit` that is
  given must name at least one host, or the call refuses (outcome `errored`).
* ONE CONTROLLER. Every ansible binary (playbook, ad-hoc, inventory, galaxy) goes
  through the same argv builder, so a containerised controller (`image=`) runs ALL of
  them — an ad-hoc probe left on the host would ask a different ansible than the one
  under test.

Container mode mounts the given paths at the SAME paths inside (configs with absolute
paths resolve unchanged), shares the host network, and forwards every environment
variable whose name starts with one of `forward_prefixes` — by pattern, so a variable
the inventory starts reading tomorrow is forwarded without anyone remembering to.

An `env` value of None UNSETS the variable (as in Proc.run) — it never reaches a child
as the string "None". In container mode it is still forwarded BY NAME (`-e VAR`, no
value): the engine passes nothing for a variable its own environment lacks, and a
subscriber can assert that everything its inventory reads is named. And a probe that reads the inventory (hostvar, group, host) REFUSES
when ansible says the inventory did not parse: ansible then falls back to the implicit
localhost and exits 0, so the probe would otherwise answer "" or [] for a broken
inventory — a wrong answer that looks exactly like a real one.

FORWARDED ARGUMENTS (`testrunner ... -- -vvv --limit n1 -e x=1`) go to every PLAYBOOK a
step runs, after its own arguments (so a forwarded `-e` wins). Never to ad-hoc calls or
inventory probes: those are the harness asking questions, not the stage doing its work.
A forwarded `--limit` NARROWS the step's own limit instead of replacing it — ansible keeps
only the LAST `--limit` given (measured, core 2.20), so appending it would silently undo a
step's targeting (a victim, the live nodes). The narrowed set is asked of ansible itself
(`ansible <step limit> --list-hosts --limit <forwarded>`, ansible's own pattern rules),
and an empty one refuses like any empty scope. A playbook the step runs WITHOUT a limit
(its own `hosts:`: a tang, the founder, a cache) runs un-narrowed: the operator's node
limit is not meant for it, and applied it would skip the play with exit 0. Options that
would make ansible exit 0 having done nothing (--check, --syntax-check, --list-*) or
change WHAT is tested (-i) are refused.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

from ..api import ERRORED, FAILED, OutcomeError, step_kind
from ..proc import Proc, Result, fill_rules


logger = logging.getLogger(__name__)   # not `log`: methods take a log= file name

# What ansible prints when an inventory source did not parse. It still exits 0 (with only
# the implicit localhost), so these lines are the ONLY sign the answer is not real.
INVENTORY_BROKEN = re.compile(r"Unable to parse .* as an inventory source|No inventory was parsed|"
                              r"Failed to parse inventory|Inventory script returned non-zero")
# A directory inventory makes ansible try EVERY entry as a source, and Python leaves a
# __pycache__ beside an inventory script the moment anything imports it. ansible warns and
# goes on with the real sources, so that one warning is noise, not an empty inventory
# (measured 2026-10-12: lab's group resolved to both nodes while the probe refused).
INVENTORY_NOISE = re.compile(r"Unable to parse \S*/__pycache__/? as an inventory source")


def inventory_broken(text: str) -> list[str]:
    """The lines saying the inventory did not parse, minus the known-harmless ones."""
    return [ln for ln in text.replace("\r", "").splitlines()
            if INVENTORY_BROKEN.search(ln) and not INVENTORY_NOISE.search(ln)]


class InventoryBroken(OutcomeError):
    def __init__(self, what: str, output: str):
        lines = [ln.strip() for ln in output.replace("\r", "").splitlines() if ln.strip()]
        why = []
        for i, ln in enumerate(lines):
            if ln.startswith("Standard error from inventory script") and i + 1 < len(lines):
                why.append(lines[i + 1])    # the script's own words: usually the real reason
        why += [ln.strip() for ln in inventory_broken(output)]
        super().__init__(ERRORED, f"{what}: the inventory did not parse, so ansible answered from an "
                                  f"EMPTY inventory — " + " | ".join(dict.fromkeys(why))[:600])


class EmptyScope(OutcomeError):
    def __init__(self, what: str):
        super().__init__(ERRORED, f"refusing to run against an empty host set ({what}): "
                                  "ansible would exit 0 having done nothing")


class ForwardRefused(OutcomeError):
    def __init__(self, why: str):
        super().__init__(ERRORED, f"arguments after `--`: {why}")


# ansible-playbook's options (core 2.20 --help). The value-taking ones: their value is never
# read as an option. The whole long list: argparse accepts any unambiguous PREFIX (`--lim`
# is --limit, measured), so a name is matched the way ansible will match it.
_VALUE_SHORT = set("ceflitTuM")
_VALUE_LONG = {"--become-method", "--become-pass-file", "--become-password-file", "--become-user",
               "--connection", "--conn-pass-file", "--connection-password-file", "--extra-vars", "--forks",
               "--inventory", "--inventory-file", "--key-file", "--limit", "--module-path", "--private-key",
               "--scp-extra-args", "--sftp-extra-args", "--skip-tags", "--ssh-common-args", "--ssh-extra-args",
               "--start-at-task", "--tags", "--timeout", "--user", "--vault-id", "--vault-pass-file",
               "--vault-password-file"}
_LONG = _VALUE_LONG | {"--ask-become-pass", "--ask-pass", "--ask-vault-pass", "--ask-vault-password", "--become",
                       "--check", "--diff", "--flush-cache", "--force-handlers", "--help", "--list-hosts",
                       "--list-tags", "--list-tasks", "--step", "--syntax-check", "--verbose", "--version"}
_NOTHING = "ansible would exit 0 having run nothing"
_REFUSED = {
    "-i": "a second inventory changes what is tested", "--inventory": "a second inventory changes what is tested",
    "--inventory-file": "a second inventory changes what is tested",
    "-C": "ansible would exit 0 having changed nothing", "--check": "ansible would exit 0 having changed nothing",
    "--syntax-check": _NOTHING, "--list-hosts": _NOTHING, "--list-tasks": _NOTHING, "--list-tags": _NOTHING,
    "-h": _NOTHING, "--help": _NOTHING, "--version": _NOTHING,
}


def _long(name: str) -> str:
    """A long option as argparse resolves it (exact, or the one option it is a prefix of)."""
    if name in _LONG:
        return name
    hits = [o for o in _LONG if o.startswith(name)]
    if len(hits) != 1:
        raise ForwardRefused(f"{name}: {'ambiguous (' + ', '.join(sorted(hits)) + ')' if hits else 'not an ansible-playbook option'}")
    return hits[0]


class Forwarded:
    """The arguments after `--`, as a playbook takes them: the limit apart, the rest as given
    (a token that also carried the limit, like -vl n1, is rebuilt without it)."""

    def __init__(self, argv: Iterable[str] = ()):
        self.argv = list(argv)
        self.limit: str | None = None
        self.args: list[str] = []
        i = 0
        while i < len(self.argv):
            start, tok = i, self.argv[i]
            i += 1
            opts: list[tuple[str, str | None]] = []       # (option, value) this token (and maybe the next) holds
            if tok.startswith("--") and len(tok) > 2:
                name, eq, value = tok.partition("=")
                name = _long(name)
                if name in _VALUE_LONG and not eq:
                    if i >= len(self.argv):
                        raise ForwardRefused(f"{name} wants a value")
                    value, i = self.argv[i], i + 1
                opts.append((name, value if name in _VALUE_LONG else None))
            elif tok.startswith("-") and len(tok) > 1 and tok != "-":
                for k, c in enumerate(tok[1:], 1):       # -vvv, -bD, -vl n1, -ln1
                    if c in _VALUE_SHORT:
                        value = tok[k + 1:]
                        if not value:
                            if i >= len(self.argv):
                                raise ForwardRefused(f"-{c} wants a value")
                            value, i = self.argv[i], i + 1
                        opts.append((f"-{c}", value))
                        break
                    opts.append((f"-{c}", None))
            else:
                raise ForwardRefused(f"{tok!r} is not an option (steps go BEFORE `--`)")
            for name, _ in opts:
                if name in _REFUSED:
                    raise ForwardRefused(f"{tok!r} is {name}: {_REFUSED[name]}")
            lim = [v for n, v in opts if n in ("-l", "--limit")]
            if not lim:
                self.args += self.argv[start:i]           # as given
                continue
            if self.limit is not None:
                raise ForwardRefused("--limit given twice (ansible would keep only the last)")
            if not lim[0].strip(", "):
                raise ForwardRefused("an empty --limit matches nothing")
            self.limit = lim[0]
            for n, v in opts:
                if n not in ("-l", "--limit"):
                    self.args += [n] if v is None else [n, v]

    def __bool__(self) -> bool:
        return bool(self.argv)


class Ansible:
    def __init__(self, proc: Proc, *, cwd: Path, inventory: Path | str, env: dict[str, str] | None = None,
                 image: str | None = None, engine: str = "podman", mounts: Iterable[Path] = (),
                 forward_prefixes: Iterable[str] = ("ANSIBLE_",), never_forward: Iterable[str] = (),
                 home: Path | None = None, forwarded: Iterable[str] | None = None):
        # the run's arguments after `--` (the step's proc carries them); refused HERE, as the
        # step's fixtures resolve, before the step's own work starts
        self.forwarded = Forwarded(getattr(proc, "forwarded", ()) if forwarded is None else forwarded)
        self.proc, self.cwd, self.inventory = proc, Path(cwd), str(inventory)
        self.env = dict(env or {})
        self.image, self.engine = image or None, engine
        self.mounts = [Path(m) for m in mounts]
        self.forward_prefixes = tuple(forward_prefixes)
        self.never_forward = set(never_forward)
        self.home = Path(home) if home else None
        logger.debug("ansible controller: %s; cwd %s, inventory %s", f"container {self.image} via {engine}"
                  if self.image else "host binaries", self.cwd, self.inventory)

    # ── the controller ──
    def argv(self, binary: str = "ansible-playbook") -> list[str]:
        """The argv PREFIX for one ansible invocation (pure: tests assert it directly)."""
        if not self.image:
            return [binary]
        argv = [self.engine, "run", "--rm", "-i", "-t", "--network=host", "--security-opt", "label=disable"]
        seen: set[str] = set()
        for m in [*self.mounts, self.cwd]:
            m = m.resolve()
            if any(m == s or m.is_relative_to(s) for s in map(Path, seen)):
                continue
            seen.add(str(m))
            argv += ["-v", f"{m}:{m}"]
        argv += ["-w", str(self.cwd)]
        if self.home:
            argv += ["-e", f"HOME={self.home}"]
        names = sorted({*os.environ, *self.env})
        for k in names:
            if k in self.never_forward:
                continue
            if k.startswith(self.forward_prefixes):
                argv += ["-e", k]
        return argv + [self.image, binary]

    def _env(self) -> dict[str, str | None]:
        # None stays None: Proc.run UNSETS it. str(None) would hand every child "None",
        # which an inventory reads as a real value (measured 2026-10-11: GATE3_SHAPE=None).
        return {k: None if v is None else str(v) for k, v in self.env.items()}

    async def _run(self, argv: list[str], **kw: Any) -> Result:
        if self.image and self.home:
            self.home.mkdir(parents=True, exist_ok=True)
        unset = sorted(k for k, v in self.env.items() if v is None)
        if unset:
            logger.debug("ansible env: unset for this call (config leaves them empty): %s", unset)
        return await self.proc.run(argv, cwd=self.cwd, env=self._env(), **kw)

    # ── scope ──
    @staticmethod
    def limit_arg(limit: Iterable[str] | str | None, what: str = "limit") -> list[str]:
        if limit is None:
            return []
        hosts = [h for h in (limit.split(",") if isinstance(limit, str) else limit) if h]
        if not hosts:
            logger.error("refusing an empty %s: ansible would exit 0 having done nothing", what)
            raise EmptyScope(what)
        return ["--limit", ",".join(hosts)]

    @staticmethod
    def extra_args(extra: dict[str, Any] | None) -> list[str]:
        out: list[str] = []
        for k, v in (extra or {}).items():
            if isinstance(v, (list, dict)):
                # a structure as JSON: `k=[...]` would reach the play as a STRING
                out += ["-e", json.dumps({k: v})]
                continue
            if isinstance(v, bool):
                v = "true" if v else "false"
            out += ["-e", f"{k}={v}"]
        return out

    # ── running ──
    async def playbook(self, playbook: str | Path, *args: str, limit: Iterable[str] | str | None = None,
                       extra: dict[str, Any] | None = None, rules: Any = None, log: str | None = None,
                       timeout: float | None = None, check: bool = True, echo: bool = True,
                       prompt_idle: float | None = None, ask_operator: bool = True) -> Result:
        """ansible-playbook PLAYBOOK [args] -i INVENTORY [--limit ...] [-e k=v ...].
        check=True turns a non-zero exit into a FAILED outcome. ask_operator=False: a prompt
        no rule answers ends the run instead of being forwarded (a gate that must NOT be passed).
        The run's forwarded arguments come last; a forwarded --limit narrows `limit` (above)."""
        if self.forwarded.limit is not None:
            if limit is None:
                logger.info("playbook %s has no limit of its own: the forwarded --limit %s does not apply",
                            playbook, self.forwarded.limit)
            else:
                limit = await self.narrow(limit, self.forwarded.limit)
        argv = [*self.argv(), str(playbook), *args, "-i", self.inventory,
                *self.limit_arg(limit), *self.extra_args(extra), *self.forwarded.args]
        logger.info("playbook %s%s%s", playbook, f" --limit {','.join(limit) if not isinstance(limit, str) else limit}"
                 if limit is not None else " (whole inventory)", f" -e {sorted(extra)}" if extra else "")
        r = await self._run(argv, rules=rules, log=log, timeout=timeout, echo=echo,
                            prompt_idle=prompt_idle, ask_operator=ask_operator)
        if check:
            self.raise_for(r, f"{Path(str(playbook)).name}")
        return r

    async def adhoc(self, pattern: str, module: str, args: str = "", *extra_argv: str,
                    limit: Iterable[str] | str | None = None, connection: str | None = None,
                    become: bool = False, log: str | None = None, echo: bool = False,
                    check: bool = False) -> Result:
        argv = [*self.argv("ansible"), "-i", self.inventory, pattern, "-m", module]
        if args:
            argv += ["-a", args]
        if connection:
            argv += ["-c", connection]
        if become:
            argv += ["-b"]
        argv += [*self.limit_arg(limit), *extra_argv]
        r = await self._run(argv, log=log, echo=echo)
        if check:
            self.raise_for(r, f"ansible {pattern} -m {module}")
        return r

    async def narrow(self, limit: Iterable[str] | str, by: str) -> list[str]:
        """The hosts of `limit` that `by` (an ansible pattern) keeps, as ansible resolves them.
        Refuses an empty result: the step would otherwise run on nothing and pass."""
        pattern = self.limit_arg(limit)[1]
        r = await self._run([*self.argv("ansible"), "-i", self.inventory, pattern, "--list-hosts",
                             "--limit", by], echo=False)
        what = f"--limit {pattern} narrowed by the forwarded --limit {by}"
        self.raise_for_inventory(r, what)
        if r.returncode != 0 and "no hosts to target" in r.text:
            logger.error("%s: no hosts left", what)
            raise EmptyScope(what)
        self.raise_for(r, what)
        lines = r.text.replace("\r", "").splitlines()
        head = next((i for i, ln in enumerate(lines) if re.match(r"\s*hosts \(\d+\):", ln)), None)
        if head is None:
            raise OutcomeError(ERRORED, f"{what}: no host list in ansible's answer: {r.text.strip()[-300:]!r}")
        hosts = [ln.strip() for ln in lines[head + 1:] if ln.startswith("    ") and ln.strip()]
        logger.info("%s -> %s", what, hosts or "NO hosts")
        if not hosts:
            raise EmptyScope(what)
        return hosts

    async def hostvar(self, host: str, expression: str) -> str:
        """Evaluate `{{ hostvars[host].<expression> }}` on the CONTROLLER (templated, unlike
        `ansible-inventory --host`, which returns group_vars untemplated)."""
        r = await self.adhoc("localhost", "debug", f"msg={{{{ hostvars['{host}'].{expression} | default('') }}}}",
                             connection="local")
        what = f"hostvars[{host!r}].{expression}"
        self.raise_for(r, what)
        self.raise_for_inventory(r, what)
        m = re.search(r'"msg":\s*"(.*)"', r.text)
        if not m:
            raise OutcomeError(ERRORED, f"{what}: no msg in ansible's answer: {r.text.strip()[-300:]!r}")
        logger.info("%s resolves to %r%s", what, m.group(1), " (undefined or empty)" if not m.group(1) else "")
        return m.group(1)

    async def inventory_host(self, host: str) -> dict:
        r = await self._run([*self.argv("ansible-inventory"), "-i", self.inventory, "--host", host], echo=False)
        self.raise_for(r, f"ansible-inventory --host {host}")
        self.raise_for_inventory(r, f"ansible-inventory --host {host}")
        return json.loads(_json_part(r.text))

    async def group(self, name: str) -> list[str]:
        """The hosts of a group as the inventory resolves it right now (empty if none)."""
        r = await self._run([*self.argv("ansible-inventory"), "-i", self.inventory, "--list"], echo=False)
        self.raise_for(r, "ansible-inventory --list")
        self.raise_for_inventory(r, f"group {name!r}")
        data = json.loads(_json_part(r.text))
        out: list[str] = []

        def walk(g: str) -> None:
            node = data.get(g) or {}
            for h in node.get("hosts", []):
                if h not in out:
                    out.append(h)
            for child in node.get("children", []):
                walk(child)
        walk(name)
        logger.info("group %s resolves to %s", name, out or "NO hosts")
        return out

    async def galaxy(self, *args: str, log: str | None = None) -> Result:
        r = await self._run([*self.argv("ansible-galaxy"), *args], log=log, echo=False)
        self.raise_for(r, "ansible-galaxy " + " ".join(args[:2]))
        return r

    async def version(self) -> str:
        r = await self._run([*self.argv(), "--version"], echo=False)
        return r.text.splitlines()[0].strip() if r.ok and r.text.strip() else ""

    @staticmethod
    def raise_for(r: Result, what: str) -> None:
        if not r.ok:
            logger.info("%s did not succeed: %s", what, "timed out" if r.timed_out else
                     f"unanswered prompt {r.unanswered!r}" if r.unanswered is not None else f"exit {r.returncode}")
        if r.timed_out:
            raise OutcomeError(FAILED, f"{what}: timed out")
        if r.unanswered is not None:
            raise OutcomeError(FAILED, f"{what}: unanswered prompt {r.unanswered!r}")
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"{what}: exit {r.returncode}")

    @staticmethod
    def raise_for_inventory(r: Result, what: str) -> None:
        if inventory_broken(r.text):
            raise InventoryBroken(what, r.text)

    def describe(self) -> str:
        return shlex.join(self.argv())


def _json_part(text: str) -> str:
    """ansible-inventory may print warnings before the JSON on a pty ("[WARNING]: ..." —
    which itself starts like a JSON array): return the text from the first line that
    actually DECODES as JSON."""
    lines = text.replace("\r", "").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("{", "[")):
            rest = "".join(lines[i:])
            try:
                json.JSONDecoder().raw_decode(rest.lstrip())
                return rest
            except json.JSONDecodeError:
                continue
    return text


def step_rules(rules: Any, root: Path) -> Any:
    """A walk step's `rules:` — a list of rules, a file (relative to the subscriber's root), or a
    template to fill: {file: <path>, NAME: value, ...} (@NAME@ placeholders, proc.fill_rules)."""
    if isinstance(rules, str):
        return Path(root) / rules
    if isinstance(rules, dict):
        values = {k: v for k, v in rules.items() if k != "file"}
        if "file" not in rules:
            raise OutcomeError(ERRORED, f"rules: a template needs file: <path> (got {sorted(rules)})")
        try:
            return fill_rules(Path(root) / str(rules["file"]), values)
        except (ValueError, OSError) as e:
            raise OutcomeError(ERRORED, f"rules {rules['file']}: {e}") from None
    return rules


@step_kind("playbook", keys=("limit", "extra", "rules", "log", "timeout", "args", "prompt_idle", "ask_operator"))
async def playbook_step(spec, ans, run):
    """A walk's `{playbook: <path>, limit, extra, rules, log, timeout, args, prompt_idle,
    ask_operator}` through the subscriber's `ans` fixture (an Ansible), every rule of Ansible
    included (no empty scope, one controller, forwarded arguments). Its Result is classified by
    the step (`outcomes:`, then the exit code), so a REFUSAL can be the expected outcome."""
    limit = spec.get("limit")
    if isinstance(limit, (str, int)):
        limit = [str(limit)]
    elif isinstance(limit, list):
        # ["{live}", cache_hosts]: a resolved list inside the list is spliced in place
        limit = [str(h) for x in limit for h in (x if isinstance(x, list) else [x])]
    rules = step_rules(spec.get("rules"), run.root)
    logger.info("playbook step %s: limit %s, extra %s, rules %s, log %s", spec["playbook"],
                limit if limit is not None else "(none: its own hosts)", sorted(spec.get("extra") or {}),
                f"{len(rules)} filled" if isinstance(rules, list) else rules or "none", spec.get("log"))
    return await ans.playbook(spec["playbook"], *[str(a) for a in spec.get("args") or []], limit=limit,
                              extra=spec.get("extra"), rules=rules, log=spec.get("log"),
                              timeout=spec.get("timeout"), check=False, prompt_idle=spec.get("prompt_idle"),
                              ask_operator=spec.get("ask_operator", True))


@step_kind("require_hosts", keys=("hint",))
async def require_hosts_step(spec, ans, step):
    """`{require_hosts: <group>, hint: <what to check>}`: refuse unless the group resolves to at
    least one host. A play that matches no hosts SUCCEEDS having done nothing — a rig once ran
    install, reboot and form in seconds without touching a node."""
    group, stage = str(spec["require_hosts"]), step.parent or step.id
    hosts = await ans.group(group)
    logger.info("%s targets group %s: %s", stage, group, hosts or "NO hosts")
    if not hosts:
        raise OutcomeError(FAILED, f"stage {stage!r} targets the group {group!r}, which resolves to NO hosts.\n"
                                   "Ansible would treat that as success while doing nothing at all."
                                   + (f" {spec['hint']}" if spec.get("hint") else ""))
