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
"""
from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Iterable

from ..api import ERRORED, FAILED, OutcomeError
from ..proc import Proc, Result


class EmptyScope(OutcomeError):
    def __init__(self, what: str):
        super().__init__(ERRORED, f"refusing to run against an empty host set ({what}): "
                                  "ansible would exit 0 having done nothing")


class Ansible:
    def __init__(self, proc: Proc, *, cwd: Path, inventory: Path | str, env: dict[str, str] | None = None,
                 image: str | None = None, engine: str = "podman", mounts: Iterable[Path] = (),
                 forward_prefixes: Iterable[str] = ("ANSIBLE_",), never_forward: Iterable[str] = (),
                 home: Path | None = None):
        self.proc, self.cwd, self.inventory = proc, Path(cwd), str(inventory)
        self.env = dict(env or {})
        self.image, self.engine = image or None, engine
        self.mounts = [Path(m) for m in mounts]
        self.forward_prefixes = tuple(forward_prefixes)
        self.never_forward = set(never_forward)
        self.home = Path(home) if home else None

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

    def _env(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.env.items()}

    async def _run(self, argv: list[str], **kw: Any) -> Result:
        if self.image and self.home:
            self.home.mkdir(parents=True, exist_ok=True)
        return await self.proc.run(argv, cwd=self.cwd, env=self._env(), **kw)

    # ── scope ──
    @staticmethod
    def limit_arg(limit: Iterable[str] | str | None, what: str = "limit") -> list[str]:
        if limit is None:
            return []
        hosts = [h for h in (limit.split(",") if isinstance(limit, str) else limit) if h]
        if not hosts:
            raise EmptyScope(what)
        return ["--limit", ",".join(hosts)]

    @staticmethod
    def extra_args(extra: dict[str, Any] | None) -> list[str]:
        out: list[str] = []
        for k, v in (extra or {}).items():
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
        no rule answers ends the run instead of being forwarded (a gate that must NOT be passed)."""
        argv = [*self.argv(), str(playbook), *args, "-i", self.inventory,
                *self.limit_arg(limit), *self.extra_args(extra)]
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

    async def hostvar(self, host: str, expression: str) -> str:
        """Evaluate `{{ hostvars[host].<expression> }}` on the CONTROLLER (templated, unlike
        `ansible-inventory --host`, which returns group_vars untemplated)."""
        r = await self.adhoc("localhost", "debug", f"msg={{{{ hostvars['{host}'].{expression} | default('') }}}}",
                             connection="local")
        m = re.search(r'"msg":\s*"(.*)"', r.text)
        return m.group(1) if m else ""

    async def inventory_host(self, host: str) -> dict:
        r = await self._run([*self.argv("ansible-inventory"), "-i", self.inventory, "--host", host], echo=False)
        self.raise_for(r, f"ansible-inventory --host {host}")
        return json.loads(_json_part(r.text))

    async def group(self, name: str) -> list[str]:
        """The hosts of a group as the inventory resolves it right now (empty if none)."""
        r = await self._run([*self.argv("ansible-inventory"), "-i", self.inventory, "--list"], echo=False)
        self.raise_for(r, "ansible-inventory --list")
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
        if r.timed_out:
            raise OutcomeError(FAILED, f"{what}: timed out")
        if r.unanswered is not None:
            raise OutcomeError(FAILED, f"{what}: unanswered prompt {r.unanswered!r}")
        if r.returncode != 0:
            raise OutcomeError(FAILED, f"{what}: exit {r.returncode}")

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
