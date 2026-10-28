"""The rig layer: config + environment, catalogue, paths, guards. See __init__.py."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Iterable

from ...api import CANNOT_TELL, ERRORED, FAILED, OutcomeError
from ...proc import Proc
from ..ansible import Ansible
from ..console import ConsoleCapture
from ..console import missing_tools as console_missing_tools
from ..machines import NOT_CREATED, MachineProvider
from ..vagrant import Vagrant
from . import config as rig_config_engine
from . import nodes as rig_nodes
from .settings import RigSettings, load_settings

PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")

# Every rig module logs through the standard library (testrunner's trace: README
# "Debugging a run"): INFO for each decision a stage takes and why, DEBUG for the detail.
log = logging.getLogger(__name__)

def die(msg: str) -> None:
    """Fail the step with a message (the bash harness's `die`). Logged with the CALLER's
    file:line, so the trace says which check refused."""
    log.error("refusing: %s", msg, stacklevel=2)
    raise OutcomeError(FAILED, msg)


def banner(text: str) -> None:
    log.info("==== %s", text, stacklevel=2)
    print(f"\n\033[1;36m==== {text} ====\033[0m", flush=True)


class _ToolDied(Exception):
    pass


def _call(fn, *args, **kw):
    """Run a tool function that reports errors with sys.exit (die): turn that into an outcome."""
    import contextlib
    import io
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return fn(*args, **kw)
    except SystemExit as e:
        log.error("rig tool %s refused (exit %s): %s", getattr(fn, "__name__", fn), e.code,
                  err.getvalue().strip() or "(no message)")
        raise OutcomeError(ERRORED, err.getvalue().strip() or f"{getattr(fn, '__name__', fn)} refused") from None
    finally:
        if err.getvalue().strip():
            log.debug("rig tool %s said on stderr: %s", getattr(fn, "__name__", fn), err.getvalue().strip())


def render_rules(template: Path, **values: str) -> list[dict]:
    """A drive-rules template with @NAME@ placeholders, filled, with drive_rules' checks:
    no placeholder left, no value the template does not use, no empty value, every
    answer ending in a carriage return (raw-mode prompts take CR, not LF, as Enter)."""
    from ...proc import fill_rules
    try:
        return fill_rules(template, values)
    except ValueError as e:
        die(str(e))


class Catalogue:
    """The rig's node catalogue, through rig_nodes (the code that validates it). The cluster
    nodes are the machines of the settings' `cluster_kind`."""

    def __init__(self, settings: RigSettings, scenario: str | None = None):
        self.settings = settings
        self.kind = settings.cluster_kind
        self.data = _call(rig_nodes.checked, _call(rig_nodes.load, str(settings.catalogue_file)), settings)
        self.machines: dict[str, dict] = self.data["machines"]
        self.scenario = scenario
        log.debug("node catalogue %s: machines %s, roles %s, scenario %s", settings.catalogue_file,
                  {n: {k: v for k, v in m.items() if k in ("kind", "late", "optional")}
                   for n, m in self.machines.items()}, self.data.get("roles"), scenario or "(none)")

    def members(self) -> list[str]:
        return list(_call(rig_nodes.scenario_members, self.data, self.scenario))

    def cluster(self) -> list[str]:              # this scenario's cluster nodes, late ones too
        return [n for n in self.members() if self.machines[n]["kind"] == self.kind]

    def cluster_nodes(self) -> list[str]:        # what `up` creates: every non-late node
        return [n for n in self.cluster() if not self.machines[n].get("late")]

    def late_nodes(self) -> list[str]:
        return [n for n in self.cluster() if self.machines[n].get("late")]

    def catalogue_cluster(self) -> list[str]:    # every cluster machine, whatever the scenario
        return [n for n, m in self.machines.items() if m["kind"] == self.kind]

    def role(self, name: str) -> list[str]:
        roles = self.data.get("roles") or {}
        if name not in roles:
            raise OutcomeError(ERRORED, f"the node catalogue has no role {name!r} (lifecycle/nodes.yml roles:)")
        v = roles[name]
        return list(v) if isinstance(v, list) else [v]

    def role1(self, name: str) -> str:
        return self.role(name)[0]

    def up_machines(self, subset: list[str] | None = None, off: Iterable[str] = ()) -> list[str]:
        """The machines `up` creates, in creation order (rig-nodes list semantics)."""
        names = self.members()
        if subset:
            allowed = [n for n in names if self.machines[n]["kind"] == self.kind and not self.machines[n].get("late")]
            for n in subset:
                if n not in allowed:
                    die(f"rig.nodes names {n}, which `up` does not create (its nodes: {' '.join(allowed)})")
            names = [n for n in names if self.machines[n]["kind"] != self.kind or n in subset]
        names = [n for n in names if not self.machines[n].get("late")]
        off = set(off)
        out = [n for n in names if self.machines[n].get("optional") not in off]
        log.info("machines to create: %s (subset %s, optional switched off %s, late nodes left out)",
                 out, subset or "none", sorted(off) or "none")
        return out


class Rig:
    """One rig checkout, as its settings (ext.rig in its runner config) describe it. A rig
    subclasses this for what its rig does differently (where the automation lives, ...)."""

    def __init__(self, root: Path, config: dict, config_file: Path | None, how: str = "default",
                 library: Path | None = None, settings: RigSettings | None = None):
        self.settings = settings or load_settings(root)
        self.PREFIX = self.settings.prefix          # the rig's environment: <PREFIX>_*
        self.NAME = self.settings.name
        self.STALE_FILES = self.settings.stale_files
        self.root = Path(root)
        self.library = Path(library) if library else self.root       # where shared rig files live
        self.config, self.config_file, self.how = config, config_file, how
        self.logs = self.root / "logs"
        self.inv = self.root / "inventory"
        self.secrets = self.root / "secrets"
        self.key = self.secrets / "harness_key"
        self.vault = self.secrets / "vault-pass"
        self.gv_secrets = self.inv / "group_vars" / "all" / "secrets.yml"
        log.debug("%s rig at %s: config %s (%s), library %s", self.NAME, self.root, config_file, how, self.library)
        self.rig_config = rig_config_engine
        self.schema = _call(self.rig_config.load_schema, self.settings)
        self.catalogue = Catalogue(self.settings, self.scenario())
        self.console = ConsoleCapture(self.logs / "console")

    # ── the run config, as the environment every tool reads ──
    def scenario(self) -> str | None:
        return None

    def flat(self, params: dict | None = None) -> dict:
        """The config, validated by the rig's own engine, with the step's run parameters."""
        sections = {s: self.config.get(s) or {} for s in self.schema["sections"]}
        if params is not None and "run" in self.schema["sections"]:
            sections["run"] = params
        flat = _call(self.rig_config.validate, sections, self.schema, str(self.config_file or "config"))
        return flat

    def fold_env(self) -> dict[str, Any]:
        """With no config chosen on purpose, a rig variable already in the environment IS
        the rig's value (config_env keeps it: `<PREFIX>_BACKEND=b ./run.sh all` builds b).
        Put it INTO the config, where a plan's `when:` and the scenario snapshot read it;
        else the plan judges the default (measured: a step guarded by `when: backend a` ran
        on a rig built with b). Typed and checked by the rig's engine, so a bad value is
        refused before the first step. With a config chosen nothing is folded: a
        contradiction is refused here, before a walk is printed that the run would refuse
        at its first step. -> {key: value} folded."""
        if self.how not in ("default", "none"):
            log.debug("fold_env: a config was chosen (%s): the environment is checked, not folded", self.how)
            self.config_env()
            return {}
        where = f"{self.config_file or 'config'} + the environment"
        current = _call(self.rig_config.env_of, self.flat(), self.schema)
        folded: dict[str, Any] = {}
        for key, spec in self.schema["keys"].items():
            have = os.environ.get(spec["env"])
            if have in (None, "") or have == current.get(spec["env"]):
                continue
            value = _call(self.rig_config.value_of_env, key, have, self.schema, where)
            cur = self.config
            *parents, leaf = key.split(".")
            for part in parents:
                nxt = cur.get(part)
                if not isinstance(nxt, dict):
                    nxt = cur[part] = {}
                cur = nxt
            cur[leaf] = value
            folded[key] = value
        if folded:
            self.flat()       # the combination too: the schema's rules see the folded values
            log.info("no config chosen on purpose (%s): the environment's values folded into the config %s",
                     self.how, {k: v for k, v in folded.items()})
        return folded

    def config_env(self, params: dict | None = None) -> dict[str, str | None]:
        env = _call(self.rig_config.env_of, self.flat(params), self.schema)
        # rig-config's rule: with a config CHOSEN on purpose, a contradicting variable in
        # the environment is REFUSED (one of the two is not what the operator meant); with
        # none chosen, a variable already in the environment is KEPT, so
        # `<PREFIX>_BACKEND=b ./run.sh all` builds b, never silently the default.
        chosen = self.how not in ("default", "none")
        conflicts = []
        kept = {}
        for var, val in env.items():
            have = os.environ.get(var)
            if have in (None, "") or val == have:
                continue
            if chosen:
                conflicts.append(f"{var}={have} in the environment, but the config says "
                                 f"{'unset' if val is None else val}")
            else:
                env[var] = have
                kept[var] = have
        if kept:
            log.info("no config chosen on purpose (%s): kept from the environment %s", self.how, kept)
        log.debug("rig environment from the config: %s",
                  {k: ("<unset>" if v is None else v) for k, v in sorted(env.items())})
        if conflicts:
            raise OutcomeError(ERRORED, "the environment contradicts the chosen config:\n  "
                               + "\n  ".join(conflicts) + "\nunset them or change the config")
        return env

    def env(self, params: dict | None = None) -> dict[str, str | None]:
        """Everything a step's commands get: the config's variables plus the rig's fixed ones."""
        env = dict(self.config_env(params))
        env.update({
            "ANSIBLE_PRIVATE_KEY_FILE": str(self.key),
            "ANSIBLE_HOST_KEY_CHECKING": "false",
            "ANSIBLE_CONFIG": str(self.root / "ansible.cfg"),
            "LIBVIRT_DEFAULT_URI": "qemu:///system",
        })
        return env

    def e(self, name: str, params: dict | None = None, default: str | None = None) -> str | None:
        """One of the rig's variables (PREFIX_name) as this step resolves it."""
        v = self.config_env(params).get(f"{self.PREFIX}_{name}")
        return default if v in (None, "") else v

    # ── the automation under test ──
    @property
    def ansible_dir(self) -> Path:
        raise NotImplementedError

    def roles_path(self) -> list[Path]:
        return [self.ansible_dir / "roles"]

    # ── controllers for a step ──
    def ansible(self, proc: Proc, params: dict | None = None) -> Ansible:
        return Ansible(proc, cwd=self.ansible_dir, inventory=self.inv, env=self.env(params),
                       forward_prefixes=(f"{self.PREFIX}_", "ANSIBLE_"))

    async def step_ansible(self, proc: Proc, params: dict | None, step: Any, run: Any, prepare: Any) -> Ansible:
        """The step's Ansible for a rig's `ans` fixture, the controller PREPARED by `await
        prepare(ans)` before the step first uses it — once per step the plan walks (a walk
        action's inner steps share their walk step's preparation: its index is the walk
        step's, `12` of `12.3`), and NEVER in a --dry-run (it changes nothing; the commands are
        described, not run). One definition for every rig, so they behave alike."""
        a = self.ansible(proc, params)
        if getattr(run, "dry", False):
            log.info("(dry) the controller is not prepared for %s", step.id)
            return a
        key = (step.parent or step.id, str(step.index).split(".")[0])
        if getattr(self, "_prepared_for", None) == key:
            log.debug("prepare: already done for step %s (#%s)", *key)
            return a
        log.debug("prepare: step %s (#%s)", *key)
        await prepare(a)
        self._prepared_for = key
        return a

    def vagrant(self, proc: Proc, params: dict | None = None) -> Vagrant:
        return Vagrant(proc, cwd=self.root, env=self.env(params))

    def machines(self, proc: Proc, params: dict | None = None) -> MachineProvider:
        """What creates this rig's machines: vagrant, unless a rig overrides this with a provider
        of its own (ext/machines.py; WP testrunner §6c.6, mode d)."""
        return self.vagrant(proc, params)

    # ── what exists ──
    async def reachable(self, ip: str, port: int = 22, timeout: float = 3.0) -> bool:
        """Does `ip:port` accept a TCP connection? The regions' probes ask this (ext/rig/regions.py:
        a machine "runs" only when it also answers). Real TCP by default — never ping (a PVE node's
        firewall drops ICMP, measured). A rig may override it, e.g. for a sandbox of fakes, where
        no real machine answers (user 2026-10-28)."""
        from .regions import answers
        return await answers(ip, port, timeout)

    async def live_nodes(self, vg: MachineProvider) -> list[str]:
        """The rig's cluster nodes that EXIST right now (asked of vagrant, in catalogue order).
        Every catalogue node is asked, not only the scenario's: a node another scenario left
        running still exists. One the Vagrantfile does not define is not created; that is only
        worth a warning for a node THIS scenario has (the Vagrantfile defines the scenario's
        machines, so a scenario without a node does not define it)."""
        states = await vg.status()
        names = self.catalogue.catalogue_cluster()
        live = [n for n in names if states.get(n, NOT_CREATED) != NOT_CREATED]
        undefined = [n for n in names if n not in states]
        missing = [n for n in undefined if n in self.catalogue.cluster()]
        if missing:
            log.warning("vagrant does not define %s, which scenario %s has (counted as not existing)",
                        missing, self.catalogue.scenario or "(default)")
        if len(undefined) > len(missing):
            log.debug("not defined by vagrant, and not in this scenario: %s",
                      [n for n in undefined if n not in missing])
        log.info("live %s nodes: %s", self.settings.cluster_noun, live or "none")
        return live

    async def live_limit(self, vg: MachineProvider) -> list[str]:
        """live_nodes, refusing to be empty: `--limit ''` matches nothing and exits 0."""
        nodes = await self.live_nodes(vg)
        if not nodes:
            die(f"no {self.settings.cluster_noun} node in this rig is created (vagrant status lists none). Refusing to run a "
                "stage against an empty host set — ansible would exit 0 having done nothing. Run `up` first.")
        return nodes

    async def require_nodes(self, vg: MachineProvider, stage: str, *nodes: str, subset: str | None = None) -> list[str]:
        """Refuse a stage hard-wired to a node this rig does not have."""
        live = await self.live_nodes(vg)
        log.debug("%s needs %s; live %s", stage, list(nodes), live)
        for n in nodes:
            if n not in live:
                die(f"{stage} needs {n}, which this rig does not have (live: {' '.join(live) or 'none'}"
                    f"{'; rig.nodes: ' + subset if subset else ''})")
        return live

    # ── bootstrap and generated files ──
    def ensure_bootstrap(self) -> None:
        """What must exist before ANY vagrant call: the Vagrantfile refuses to load
        without the harness public key."""
        self.secrets.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        if not self.key.exists():
            log.info("no harness key at %s: generating one", self.key)
            print("generating harness SSH keypair")
            import subprocess
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key),
                            "-C", f"{self.NAME}-harness"], stdin=subprocess.DEVNULL, check=True)

    def ensure_secrets(self) -> None:
        if not self.vault.exists():
            log.info("no vault password at %s: generating a throwaway one", self.vault)
            print("generating throwaway vault password")
            old = os.umask(0o077)
            try:
                self.vault.write_text(base64.b64encode(os.urandom(24)).decode() + "\n")
            finally:
                os.umask(old)
        if not self.gv_secrets.exists():
            log.info("no %s: seeding it from the example", self.gv_secrets)
            print("seeding group_vars/all/secrets.yml from example")
            shutil.copy(self.gv_secrets.with_name("secrets.yml.example"), self.gv_secrets)

    def ensure_config(self) -> None:
        """The harness ansible.cfg: ABSOLUTE roles_path, a full log under logs/, and NO
        vault_password_file (the harness is self-contained; a role that passes its own file
        beside a global one makes ansible-vault refuse with two 'default' vault-ids).
        AddKeysToAgent=no + IdentityAgent=none: a measured leak of the throwaway key into
        the operator's real agent (tests/agent-guard asserts both)."""
        roles = ":".join(str(p) for p in self.roles_path())
        log.debug("writing %s (roles_path %s, callbacks from %s)", self.root / "ansible.cfg", roles,
                  self.ansible_dir / "callback_plugins")
        (self.root / "ansible.cfg").write_text(f"""\
# Generated by the harness — do not edit; regenerated on each run.
[defaults]
roles_path = {roles}
host_key_checking = False
retry_files_enabled = False
interpreter_python = auto_silent
inventory_ignore_patterns = __pycache__
log_path = {self.logs}/ansible.log
callbacks_enabled = ansible.posix.profile_tasks, ansible.posix.timer
callback_plugins = {self.ansible_dir}/callback_plugins
stdout_callback = progress

[privilege_escalation]
become = true
become_method = sudo

[ssh_connection]
ssh_args = -o ControlMaster=auto -o ControlPersist=60s -o IdentitiesOnly=yes -o AddKeysToAgent=no -o IdentityAgent=none
timeout = 30
""")

    def drive_rules_check(self, files: Iterable[Path]) -> None:
        """Every drive rule ends its answer with a CARRIAGE RETURN, or the stage hangs at the
        prompt forever (pause takes CR, not LF) — cheap to assert, expensive to diagnose."""
        files = list(files)
        for f in files:
            for r in json.loads(Path(f).read_text()):
                if not r["send"].endswith("\r"):
                    die(f"{f}: send={r['send']!r} has no trailing \\r")
        log.debug("drive rules checked (every answer ends in CR): %s", [Path(f).name for f in files])

    async def observe_rig(self, state: dict) -> str:
        """The `rig` state as reality says: no cluster node exists -> absent; otherwise the
        model's value stands (installed vs clustered cannot be told apart without a rig
        round-trip), never below `up`. When nodes exist and the model has NO value (unknown,
        or never stored) this observer cannot tell which value it is, and says so: CANNOT_TELL
        keeps the variable unknown (measured 2026-10-28: answering `up` there stored a false
        value over a rig with PVE installed). The harness key is ensured FIRST: the
        Vagrantfile refuses to load without it, so on a fresh checkout `vagrant status` would
        fail."""
        self.ensure_bootstrap()
        p = Proc(self.root, self.logs)
        live = await self.live_nodes(self.machines(p))
        if not live:
            log.info("observe rig: no %s node exists -> absent (the model said %r)", self.settings.cluster_noun,
                     state.get("rig"))
            return "absent"
        have = state.get("rig")
        if have is None:
            log.info("observe rig: %s exist and the model has no value -> cannot tell which value", live)
            return CANNOT_TELL
        seen = have if have != "absent" else "up"
        log.info("observe rig: %s exist -> %s (%s)", live, seen,
                 "the model's value stands" if seen == have else f"the model said {have!r}, nodes exist: at least up")
        return seen

    async def node_ip(self, ans: Ansible, node: str) -> str:
        """A node's harness IP, read from the inventory rather than restated."""
        hv = await ans.inventory_host(node)
        if "ansible_host" not in hv:
            die(f"could not resolve ansible_host for {node} from the inventory")
        log.debug("%s: ansible_host %s", node, hv["ansible_host"])
        return hv["ansible_host"]

    async def ssh_cmd(self, ans: Ansible, node: str, remote: str, connect_timeout: int | None = 10) -> str:
        """A shell command running `remote` on a node over plain SSH as root, with the harness
        identity only: the agent is never consulted nor written to (IdentityAgent=none,
        AddKeysToAgent=no — the measured key leak)."""
        ip = await self.node_ip(ans, node)
        ct = f"-o ConnectTimeout={connect_timeout} " if connect_timeout else ""
        return (f"ssh -i {self.key} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                f"-o LogLevel=ERROR -o IdentitiesOnly=yes -o AddKeysToAgent=no -o IdentityAgent=none "
                f"{ct}root@{ip} {shlex.quote(remote)}")

    async def domains_left(self, proc: Proc) -> list[str]:
        """The rig's libvirt domains that still EXIST, whether or not the Vagrantfile defines
        them now: every machine vagrant ever created here, plus vagrant-libvirt's
        "<dir>_<machine>" names. `destroy` refuses to report success while any is left."""
        names: set[str] = set()
        for idf in (self.root / ".vagrant" / "machines").glob("*/libvirt/id"):
            if idf.stat().st_size:
                r = await proc.run(["virsh", "domname", idf.read_text().strip()], echo=False)
                if r.returncode == 0 and r.text.strip():
                    names.add(r.text.strip())
                    log.debug("%s: vagrant's id still names domain %s", idf.parent.parent.name, r.text.strip())
        r = await proc.run(["virsh", "list", "--all", "--name"], echo=False)
        if r.returncode != 0:
            # an empty answer from a FAILED list would read as "nothing left": destroy would
            # report success over domains that still exist
            die(f"virsh list failed (exit {r.returncode}) — cannot tell which rig domains are left")
        prefix = self.root.name + "_"
        names |= {ln.strip() for ln in r.text.splitlines() if ln.strip().startswith(prefix)}
        left = sorted(n for n in names if n)
        log.info("rig domains left: %s", left or "none")
        return left

    # ── stale per-node state (the settings' stale_files) ──
    async def purge_stale_node_state(self, vg: MachineProvider) -> None:
        """A node about to be CREATED gets blank disks: anything the controller still holds
        about an earlier node of that name is stale, and stale is not harmless (measured: a
        leftover recovery key made an encryption role skip provisioning entirely). Purged per node, and
        only for nodes not created — for one that exists, the key matches its disks."""
        states = await vg.status()
        for name, state in states.items():
            if state != "not_created":
                log.debug("stale state: %s exists (%s) — its host_vars stay (they match its disks)", name, state)
                continue
            hv = self.inv / "host_vars" / name
            found = [f for rel in self.STALE_FILES for f in sorted(hv.glob(rel + "*"))]
            log.info("stale state: %s not created — purging %s", name,
                     [str(f.relative_to(self.inv)) for f in found] or "nothing (none found)")
            for rel in self.STALE_FILES:
                for f in sorted(hv.glob(rel + "*")):
                    print(f"purging stale {rel} for {name} (not created yet — its disks will be blank)")
                    f.unlink()

    # ── host tools, checked BEFORE anything is built ──
    def check_host_tools(self, params: dict | None = None) -> None:
        """Refuse up front when this host lacks a tool a later part of the stage runs.
        Measured 2026-10-11: on a host without `script` (util-linux), `up` built every VM
        and only THEN died starting the console capture."""
        missing = console_missing_tools() if self.console_enabled(params) else []
        log.info("host tools for this stage: %s", "; ".join(missing) or "all present")
        if missing:
            die("this host lacks tools the rig needs — nothing was built:\n  " + "\n  ".join(missing)
                + f"\nInstall them, or switch the serial console capture off (rig.console: \"off\", "
                  f"or {self.PREFIX}_CONSOLE=off)")

    # ── console capture ──
    def console_enabled(self, params: dict | None = None) -> bool:
        return (self.e("CONSOLE", params, "log") or "log") == "log"

    def console_start(self, params: dict | None = None) -> None:
        if not self.console_enabled(params):
            log.info("console capture off (%s_CONSOLE=%s)", self.PREFIX, self.e("CONSOLE", params))
            return
        for n in self.catalogue.catalogue_cluster():
            idf = self.root / ".vagrant" / "machines" / n / "libvirt" / "id"
            if not idf.is_file():
                log.debug("console %s: no libvirt id (not created) — nothing to capture", n)
                continue
            if self.console.start(n, idf.read_text().strip()):
                print(f"console capture: {n} -> logs/console/{n}.log")

    def console_stop(self) -> None:
        self.console.stop_all()
