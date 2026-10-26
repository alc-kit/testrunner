"""rig-config: run configuration for a Vagrant rig: select, validate, resolve. GENERIC:
what a rig's knobs are lives in that rig's <configs>/schema.yml, so this engine never names
one. Part of testrunner.ext.rig; a rig calls it as `python -m testrunner.ext.rig.config
--root <rig> ...` (its bin/rig-config is a wrapper), or imports it.

A config (configs/<name>.yml) describes a run in parts, each a mapping:
  rig:      what the rig IS. Build-time: takes effect when the VMs are created or
            installed. `lock-write` (run.sh: at `up`) records it; `lock-diff` reports
            how a later config differs, so the harness can warn.
  harness:  how the harness runs.
  run:      per-step settings, the defaults of every plan step.
  plan:     what `./run.sh` with no stage arguments runs: an ordered list of
            {stages: [...] | "a..b", with: {<run key>: value}}.
  edit:     testrunner's walk edits (repeat/insert/skip/replace/retry), checked there; here
            only their shape and the `with` of the steps they add (run keys).
`extends: <name>` lays a config over another (chains allowed, cycles refused;
mappings merge key by key, `plan` is replaced whole, `edit` accumulates, parent first). configs/default.yml holds EVERY
key. Machine-specific settings go in a gitignored configs/local/<name>.yml that
extends a committed config (`--config local/<name>`).

Which file: --config FILE|NAME, else $<config_env>, else ./<default_link> (a
gitignored symlink), else configs/default.yml — <config_env> and <default_link> come
from the schema.

configs/schema.yml:
  config_env: MYRIG_CONFIG          # env var naming the config
  default_link: myrig.yml           # the symlink at the repo root
  sections: [rig, harness, run]
  keys:
    rig.networking: {type: str, allowed: [flat, vlan], env: MYRIG_NETWORKING}
    rig.shape: {type: str, allowed_from: {file: lifecycle/nodes.yml, key: shapes}, env: MYRIG_SHAPE}
    rig.automation.source: {type: path, env: MYRIG_AUTOMATION_SRC}  # made absolute
    rig.cache.enabled: {type: bool, env: MYRIG_CACHE}               # true -> 1, false -> 0
    run.negative_control: {type: bool, env: MYRIG_NEGATIVE, when_false: unset}
    harness.victims: {type: list, env: MYRIG_VICTIMS}              # space-joined
    rig.upstream.ref: {type: str, env: MYRIG_UPSTREAM_REF, lock: false}  # never compared by lock-diff
  rules:                            # combinations the rig cannot honour
    - {when: {rig.shape: set}, require: {rig.networking: vlan}, why: "..."}
    - {when: {rig.storage.topology: set}, forbid: {rig.storage.backend: mixed}, why: "..."}
  (a condition value `set` means "not null"; otherwise it is compared as a string)

The output is the environment the rig has always read — the config is a front end,
nothing behind it is rewired. With a config CHOSEN (cli/env/symlink) a variable
already in the environment that DIFFERS from the config is REFUSED: two answers in
one invocation are never resolved silently. With none chosen, the environment wins.

  rig-config select [--config X]                    -> "<path>\\t<cli|env|symlink|default>"
  rig-config env  --file F --how H [--with k=v]... [--fresh]  -> shell `export`/`unset` lines
  rig-config show --file F [--with k=v]...          -> the resolved config (YAML)
  rig-config plan --file F                          -> per step: "<stages>\\t<k=v ...>"
  rig-config lock-write --file F --lock L
  rig-config lock-diff  --file F --lock L           -> "<key>: built=<a> now=<b>" lines
"""
import argparse
import copy
import datetime
import json
import os
import shlex
import subprocess
import sys

import yaml

TYPES = ("str", "int", "bool", "list", "path")


def die(msg):
    sys.stderr.write("rig-config: %s\n" % msg)
    sys.exit(2)


def load_yaml(path, what="config"):
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except OSError as e:
        die("cannot read %s %s: %s" % (what, path, e))
    except yaml.YAMLError as e:
        die("%s is not valid YAML: %s" % (path, e))
    if not isinstance(data, dict):
        die("%s: a %s is a mapping" % (path, what))
    return data


# ── the schema ────────────────────────────────────────────────────────────────
def load_schema(settings):
    """The rig's schema; it carries the rig's root and configs directory (`_root`,
    `_configs`), so every function that is given the schema knows which rig it serves."""
    path = str(settings.schema_file)
    s = load_yaml(path, "schema")
    for k in ("config_env", "default_link", "sections", "keys"):
        if k not in s:
            die("%s: missing `%s`" % (path, k))
    for key, spec in s["keys"].items():
        if not isinstance(spec, dict) or spec.get("type") not in TYPES or not spec.get("env"):
            die("%s: key %s needs {type: %s, env: VAR}" % (path, key, "|".join(TYPES)))
        odd = sorted(str(k) for k in spec if k not in ("type", "env", "allowed", "allowed_from", "when_false", "lock"))
        if odd:  # e.g. `false:`, which YAML reads as the boolean False and nothing would match
            die("%s: key %s has unknown field(s) %s" % (path, key, ", ".join(odd)))
        if key.split(".", 1)[0] not in s["sections"]:
            die("%s: key %s is not under a declared section" % (path, key))
    s.setdefault("rules", [])
    s["_root"], s["_configs"] = str(settings.root), str(settings.configs_dir)
    return s


def allowed(spec, schema):
    if "allowed" in spec:
        return [str(v) for v in spec["allowed"]]
    src = spec.get("allowed_from")
    if src:
        data = load_yaml(os.path.join(schema["_root"], src["file"]), "file")
        return sorted(str(k) for k in (data.get(src["key"]) or {}))
    return None


# ── loading ───────────────────────────────────────────────────────────────────
def name_to_path(x, schema):
    if x.endswith((".yml", ".yaml")):
        return os.path.abspath(x)
    return os.path.join(schema["_configs"], x + ".yml")   # NAME, or local/NAME


def merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k == "edit":                  # walk edits accumulate, as testrunner does
            out[k] = list(out.get(k) or []) + copy.deepcopy(list(v or []))
        elif k == "plan" or not (isinstance(v, dict) and isinstance(out.get(k), dict)):
            out[k] = copy.deepcopy(v)
        else:
            out[k] = merge(out[k], v)
    return out


def load(path, schema, chain=()):
    if path in chain:
        die("extends cycle: %s" % " -> ".join(chain + (path,)))
    data = load_yaml(path)
    # actions/uses: walk actions the run config defines or imports (testrunner's own loader
    # collects them); this loader only has to let them through
    top = set(schema["sections"]) | {"extends", "plan", "edit", "actions", "uses"}
    unknown = sorted(set(data) - top)
    if unknown:
        die("%s: unknown top-level key(s) %s (known: %s)" % (path, ", ".join(unknown), ", ".join(sorted(top))))
    parent = data.pop("extends", None)
    if parent is not None:
        data = merge(load(name_to_path(str(parent), schema), schema, chain + (path,)), data)
    return data


def flatten(d, prefix, keys):
    """{'nic': {'model': 'igb'}} under 'rig.' -> {'rig.nic.model': 'igb'}: a mapping is
    descended into only where the schema has keys below it; anything else is a leaf."""
    out = {}
    for k, v in d.items():
        p = prefix + str(k)
        if isinstance(v, dict) and any(s.startswith(p + ".") for s in keys):
            out.update(flatten(v, p + ".", keys))
        else:
            out[p] = v
    return out


# ── validation ────────────────────────────────────────────────────────────────
def check(key, v, schema, where):
    spec = schema["keys"][key]
    t = spec["type"]
    if v is None:
        return
    ok = {"int": isinstance(v, int) and not isinstance(v, bool),
          "bool": isinstance(v, bool),
          "str": isinstance(v, (str, int)) and not isinstance(v, bool),
          "path": isinstance(v, str),
          "list": isinstance(v, list) and all(isinstance(i, str) for i in v)}[t]
    if not ok:
        die("%s: %s must be %s %s (got %r)" % (where, key, "an" if t == "int" else "a", t, v))
    al = allowed(spec, schema)
    # A list's allowed values constrain each ITEM (str() of a whole list never matches).
    bad = [i for i in v if i not in al] if (al is not None and t == "list") else \
          ([v] if (al is not None and str(v) not in al) else [])
    if bad:
        die("%s: %s must be one of %s (got %r)" % (where, key, ", ".join(al) or "<none declared>",
                                                  bad[0] if t == "list" else v))


def holds(cond, flat):
    for k, want in cond.items():
        have = flat.get(k)
        if want == "set":
            if have is None:
                return False
        elif str(have) != str(want):
            return False
    return True


def validate(cfg, schema, where):
    keys = schema["keys"]
    flat = {}
    for sect in schema["sections"]:
        sub = cfg.get(sect) or {}
        if not isinstance(sub, dict):
            die("%s: %s must be a mapping" % (where, sect))
        flat.update(flatten(sub, sect + ".", keys))
    unknown = sorted(k for k in flat if k not in keys)
    if unknown:
        die("%s: unknown key(s) %s" % (where, ", ".join(unknown)))
    for k, v in flat.items():
        check(k, v, schema, where)
    rules(flat, schema, where)
    plan = cfg.get("plan") or []
    if not isinstance(plan, list):
        die("%s: plan must be a list of steps" % where)
    for i, step in enumerate(plan, 1):
        if not isinstance(step, dict) or "stages" not in step or set(step) - {"stages", "with"}:
            die("%s: plan step %d must be {stages: ..., with: {...}}" % (where, i))
        st = step["stages"]
        if not (isinstance(st, str) or (isinstance(st, list) and st and all(isinstance(x, str) for x in st))):
            die("%s: plan step %d: stages is a list of stage names or one \"a..b\" range" % (where, i))
        for k, v in (step.get("with") or {}).items():
            if "run." + str(k) not in keys:
                die("%s: plan step %d: `with` takes run keys only (%s); got %s"
                    % (where, i, ", ".join(runkeys(schema)), k))
            check("run." + str(k), v, schema, "%s plan step %d" % (where, i))
            rules(dict(flat, **{"run." + str(k): v}), schema, "%s plan step %d" % (where, i))
    edits = cfg.get("edit") or []
    if not isinstance(edits, list) or not all(isinstance(e, dict) for e in edits):
        die("%s: edit must be a list of walk edits ({repeat|insert|skip|replace|retry: ...})" % where)
    for i, e in enumerate(edits, 1):
        added = [s for k in ("insert", "by") if isinstance(e.get(k), list) for s in e[k]]
        for step in added:
            for k, v in ((step.get("with") or {}).items() if isinstance(step, dict) else ()):
                if "run." + str(k) not in keys:
                    die("%s: edit %d: `with` takes run keys only (%s); got %s"
                        % (where, i, ", ".join(runkeys(schema)), k))
                check("run." + str(k), v, schema, "%s edit %d" % (where, i))
    return flat


def rules(flat, schema, where):
    for r in schema["rules"]:
        if not holds(r.get("when") or {}, flat):
            continue
        if "require" in r and not holds(r["require"], flat):
            die("%s: %s" % (where, r.get("why") or "rule %s" % r))
        if "forbid" in r and holds(r["forbid"], flat):
            die("%s: %s" % (where, r.get("why") or "rule %s" % r))


def runkeys(schema):
    return [k[4:] for k in schema["keys"] if k.startswith("run.")]


def apply_with(flat, withs, schema, where):
    out = dict(flat)
    for w in withs or []:
        if "=" not in w:
            die("--with takes key=value (got %r)" % w)
        k, v = w.split("=", 1)
        key = "run." + k
        if key not in schema["keys"]:
            die("--with %s: not a run key (%s)" % (k, ", ".join(runkeys(schema))))
        t = schema["keys"][key]["type"]
        if t == "bool":
            if v not in ("true", "false"):
                die("--with %s takes true or false" % k)
            v = v == "true"
        elif t == "int":
            try:
                v = int(v)
            except ValueError:
                die("--with %s takes an integer" % k)
        elif v == "null":
            v = None
        check(key, v, schema, "--with")
        out[key] = v
    rules(out, schema, "--with")
    return out


def resolved(path, schema, withs=None):
    cfg = load(path, schema)
    return cfg, apply_with(validate(cfg, schema, path), withs, schema, path)


# ── output ────────────────────────────────────────────────────────────────────
def env_of(flat, schema):
    """-> {VAR: value-or-None}; None = unset."""
    out = {}
    for k, spec in schema["keys"].items():
        v = flat.get(k)
        t = spec["type"]
        if v is None or (t == "bool" and v is False and spec.get("when_false") == "unset"):
            out[spec["env"]] = None
        elif t == "bool":
            out[spec["env"]] = "1" if v else "0"
        elif t == "list":
            out[spec["env"]] = " ".join(v)
        elif t == "path":
            out[spec["env"]] = os.path.abspath(os.path.join(schema["_root"], os.path.expanduser(v)))
        else:
            out[spec["env"]] = str(v)
    return out


def value_of_env(key, raw, schema, where):
    """The reverse of env_of for one key: the config value an environment string stands
    for, checked like a config value. A string env_of could never have produced is refused."""
    spec = schema["keys"][key]
    t = spec["type"]
    if t == "bool":
        if raw not in ("0", "1"):
            die("%s: %s=%r: %s is a bool, written 1 or 0" % (where, spec["env"], raw, key))
        v = raw == "1"
    elif t == "int":
        try:
            v = int(raw)
        except ValueError:
            die("%s: %s=%r: %s is an int" % (where, spec["env"], raw, key))
    elif t == "list":
        v = raw.split()
    elif t == "path":
        v = os.path.abspath(os.path.expanduser(raw))   # relative to where it was typed
    else:
        v = raw
    check(key, v, schema, where)
    return v


def cmd_select(a, schema):
    if a.config:
        path, how = name_to_path(a.config, schema), "cli"
    elif os.environ.get(schema["config_env"]):
        path, how = name_to_path(os.environ[schema["config_env"]], schema), "env"
    else:
        link = os.path.join(schema["_root"], schema["default_link"])
        if os.path.lexists(link):
            if not os.path.exists(link):
                die("./%s is a dangling symlink (-> %s)" % (schema["default_link"], os.readlink(link)))
            path, how = os.path.realpath(link), "symlink"
        else:
            path, how = os.path.join(schema["_configs"], "default.yml"), "default"
    if not os.path.exists(path):
        die("no such config: %s (from %s)" % (path, how))
    print("%s\t%s" % (path, how))


def cmd_env(a, schema):
    _, flat = resolved(a.file, schema, a.with_)
    chosen = a.how != "default"
    lines, conflicts = [], []
    for var, val in sorted(env_of(flat, schema).items()):
        # --fresh: a plan step spawned by the harness from a config its parent already
        # checked — the parent's exports are the step defaults, not a second answer
        have = None if a.fresh else os.environ.get(var)
        if have not in (None, "") and val != have:
            if chosen:
                conflicts.append("%s=%s in the environment, but the config says %s"
                                 % (var, have, "unset" if val is None else val))
            else:
                lines.append("# %s=%s kept from the environment (no config chosen)" % (var, have))
            continue
        if val is None:
            if have is not None or a.fresh:
                lines.append("unset %s" % var)
        else:
            lines.append("export %s=%s" % (var, shlex.quote(val)))
    if conflicts:
        die("the environment contradicts %s:\n  %s\nunset them or change the config; a run never "
            "picks one of two answers silently" % (a.file, "\n  ".join(conflicts)))
    print("\n".join(lines))


def cmd_show(a, schema):
    cfg, flat = resolved(a.file, schema, a.with_)
    nested = {}
    for k in schema["keys"]:
        cur = nested
        parts = k.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = flat.get(k)
    nested["plan"] = cfg.get("plan") or []
    if cfg.get("edit"):
        nested["edit"] = cfg["edit"]
    yaml.safe_dump(nested, sys.stdout, sort_keys=False, default_flow_style=False)


def cmd_plan(a, schema):
    cfg, _ = resolved(a.file, schema)
    for step in cfg.get("plan") or []:
        st = step["stages"] if isinstance(step["stages"], str) else " ".join(step["stages"])
        w = " ".join("%s=%s" % (k, "true" if v is True else "false" if v is False else "null" if v is None else v)
                     for k, v in (step.get("with") or {}).items())
        print("%s\t%s" % (st, w))


def rig_of(flat):
    return {k: flat.get(k) for k in flat if k.startswith("rig.")}


def cmd_lock_write(a, schema):
    _, flat = resolved(a.file, schema)
    rec = {"config": a.file,
           "written": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "rig": {k: flat.get(k) for k in schema["keys"] if k.startswith("rig.")}}
    for k, spec in schema["keys"].items():   # the commit of every tree the rig was built from
        if k.startswith("rig.") and spec["type"] == "path" and flat.get(k):
            p = os.path.abspath(os.path.join(schema["_root"], os.path.expanduser(flat[k])))
            r = subprocess.run(["git", "-C", p, "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
            rec.setdefault("commits", {})[k] = r.stdout.strip() if r.returncode == 0 else None
    with open(a.lock, "w", encoding="utf-8") as fh:
        yaml.safe_dump(rec, fh, sort_keys=False)


def cmd_lock_diff(a, schema):
    if not os.path.exists(a.lock):
        return
    with open(a.lock, encoding="utf-8") as fh:
        built = (yaml.safe_load(fh) or {}).get("rig") or {}
    _, flat = resolved(a.file, schema)
    now = {k: flat.get(k) for k in schema["keys"] if k.startswith("rig.")}
    for k in sorted(set(built) | set(now)):
        # re-testing a newer tree (or another ref) on the same rig is normal — the
        # banner names it: a path is never compared, nor a key marked `lock: false`
        spec = schema["keys"].get(k, {})
        if spec.get("type") == "path" or spec.get("lock") is False:
            continue
        if built.get(k) != now.get(k):
            print("%s: built=%s now=%s" % (k, json.dumps(built.get(k)), json.dumps(now.get(k))))


def main(argv=None):
    from .settings import load_settings
    from ...config import ConfigError
    argv = list(sys.argv[1:] if argv is None else argv)
    root = None
    if argv[:1] == ["--root"]:
        if len(argv) < 2:
            die("--root needs a directory")
        root, argv = argv[1], argv[2:]
    try:
        settings = load_settings(root)
    except ConfigError as e:
        die(str(e))
    ap = argparse.ArgumentParser(prog="rig-config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("select").add_argument("--config")
    for name in ("env", "show", "plan", "lock-write", "lock-diff"):
        p = sub.add_parser(name)
        p.add_argument("--file", required=True)
        if name == "env":
            p.add_argument("--how", required=True)
            p.add_argument("--fresh", action="store_true")
        if name in ("env", "show"):
            p.add_argument("--with", dest="with_", action="append", default=[])
        if name.startswith("lock"):
            p.add_argument("--lock", required=True)
    a = ap.parse_args(argv)
    schema = load_schema(settings)
    {"select": cmd_select, "env": cmd_env, "show": cmd_show, "plan": cmd_plan,
     "lock-write": cmd_lock_write, "lock-diff": cmd_lock_diff}[a.cmd](a, schema)
    return 0


if __name__ == "__main__":
    sys.exit(main())
