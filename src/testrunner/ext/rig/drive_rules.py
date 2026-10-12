"""drive-rules: render and check the pty-drive rule files that answer the playbooks'
confirmation gates. Part of testrunner.ext.rig; a rig calls it as `python -m
testrunner.ext.rig.drive_rules ...` (its bin/drive-rules is a wrapper).

A rule file is a JSON list of {"expect": <regex>, "send": <text>}. The committed files
are TEMPLATES: anything that names a node or a cluster is a placeholder (@VICTIM@,
@CLUSTER@, ...) filled at run time from the node catalogue and the inventory — a token
baked into a file is a token that goes stale the day a role moves to another node.

  drive-rules check <file>...                       every send ends with a carriage return
  drive-rules render <template> <out> [NAME=value]...
                                                    fill @NAME@ placeholders, write <out>

render REFUSES — rather than writing rules that would hang or answer wrongly — when:
  * a placeholder is left unfilled (the gate would be answered with "@VICTIM@");
  * a NAME is given that the template does not use (a caller that thinks it is
    setting the token, and is not);
  * a value is empty;
  * any send lacks the trailing carriage return. ansible.builtin.pause treats CR, not
    LF, as Enter: "YES" without "\\r" is typed and never submitted, and the stage hangs
    at the prompt forever, with no error and no timeout.
"""
import json
import re
import sys

PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")


def die(msg, code=1):
    print("drive-rules: " + msg, file=sys.stderr)
    sys.exit(code)


def load(path):
    try:
        with open(path) as fh:
            rules = json.load(fh)
    except (OSError, ValueError) as e:
        die("%s: %s" % (path, e))
    if not isinstance(rules, list) or not rules or not all(
            isinstance(r, dict) and isinstance(r.get("expect"), str) and isinstance(r.get("send"), str)
            for r in rules):
        die("%s: must be a non-empty list of {expect, send} strings" % path)
    return rules


def no_cr(rules):
    return [r["send"] for r in rules if not r["send"].endswith("\r")]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    cmd, args = argv[0], argv[1:]
    if cmd == "check":
        if not args:
            die("check needs at least one file", 2)
        bad = 0
        for path in args:
            for send in no_cr(load(path)):
                print("drive-rules: %s: send=%r has no trailing \\r" % (path, send), file=sys.stderr)
                bad += 1
        sys.exit(1 if bad else 0)
    if cmd == "render":
        if len(args) < 2:
            die("render needs <template> <out> [NAME=value]...", 2)
        template, out, pairs = args[0], args[1], args[2:]
        rules = load(template)
        values = {}
        for p in pairs:
            name, sep, value = p.partition("=")
            if not sep or not PLACEHOLDER.fullmatch("@%s@" % name):
                die("%r is not NAME=value (NAME in capitals)" % p, 2)
            if not value:
                die("%s is empty — refusing to answer a gate with nothing" % name)
            values[name] = value
        used = {m for r in rules for k in ("expect", "send") for m in PLACEHOLDER.findall(r[k])}
        unused = sorted(set(values) - used)
        if unused:
            die("%s does not use %s — check the template and the caller" % (template, ", ".join(unused)))
        missing = sorted(used - set(values))
        if missing:
            die("%s needs %s — refusing to leave a placeholder in the answers" % (template, ", ".join(missing)))
        filled = [{**r, **{k: PLACEHOLDER.sub(lambda m: values[m.group(1)], r[k]) for k in ("expect", "send")}}
                  for r in rules]
        bad = no_cr(filled)
        if bad:
            die("%s: send=%r has no trailing \\r" % (template, bad[0]))
        with open(out, "w") as fh:
            json.dump(filled, fh, indent=2)
            fh.write("\n")
        print("drive-rules: %s%s" % (out, "".join(" %s=%s" % kv for kv in sorted(values.items()))))
        return
    die("unknown command %r" % cmd, 2)


if __name__ == "__main__":
    main()
