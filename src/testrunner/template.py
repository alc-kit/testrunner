"""Templates in walk actions: `{name}` and `{name:arg}` references inside string values.

One small grammar, deliberately NOT a second Jinja: no expressions, no filters, no
conditionals. A reference names a value; what a value means is decided by whoever
resolves the name:

  {param.limit}             a parameter (the walk's `params:` defaults, under the caller's)
  {env.HOME}                the runner's environment
  {run.root} {run.id}       facts of the run
  {step.id} {step.parent}   the inner step that is rendering
  {state.rig}               the current value of a state variable
  {live} {role:founder}     a RESOLVER an extension or the subscriber registered

`{{` and `}}` are literal braces. An argument may itself hold references
(`{inventory:{param.founder}.cluster_name}`), resolved first. A string that is EXACTLY one
reference becomes the resolved value itself (a list stays a list, a number a number);
inside a longer string a value is joined as text, and `escape` (re.escape, for a regular
expression) is applied to it — never to the literal text around it.
"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")

Resolve = Callable[[str, str], Awaitable[Any]]


class TemplateError(Exception):
    pass


def _parse(text: str) -> list[tuple[str, Any]]:
    """[("text", str) | ("ref", (name, arg_parts))]; arg_parts is a parse of the argument."""
    out: list[tuple[str, Any]] = []
    buf: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "{" and text.startswith("{{", i):
            buf.append("{")
            i += 2
            continue
        if c == "}" and text.startswith("}}", i):
            buf.append("}")
            i += 2
            continue
        if c == "}":
            raise TemplateError(f"{text!r}: a lone `}}` (write `}}}}` for a literal brace)")
        if c != "{":
            buf.append(c)
            i += 1
            continue
        depth, j = 1, i + 1
        while j < n and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        if depth:
            raise TemplateError(f"{text!r}: an unclosed `{{` (write `{{{{` for a literal brace)")
        inner = text[i + 1:j - 1]
        name, sep, arg = inner.partition(":")
        if not NAME.match(name):
            raise TemplateError(f"{text!r}: {{{inner}}} does not start with a name")
        if buf:
            out.append(("text", "".join(buf)))
            buf = []
        out.append(("ref", (name, _parse(arg) if sep else None)))
        i = j
    if buf:
        out.append(("text", "".join(buf)))
    return out


def references(value: Any) -> list[str]:
    """Every reference name in a value (recursively), for checks before anything runs."""
    names: list[str] = []

    def walk_parts(parts):
        for kind, x in parts:
            if kind == "ref":
                names.append(x[0])
                if x[1] is not None:
                    walk_parts(x[1])

    def walk(v):
        if isinstance(v, str):
            walk_parts(_parse(v))
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
    walk(value)
    return names


async def render(value: Any, resolve: Resolve, escape: Callable[[str], str] | None = None) -> Any:
    """Render every string in `value` (lists and mapping VALUES too; keys stay literal)."""
    if isinstance(value, str):
        return await render_str(value, resolve, escape)
    if isinstance(value, list):
        return [await render(v, resolve, escape) for v in value]
    if isinstance(value, dict):
        return {k: await render(v, resolve, escape) for k, v in value.items()}
    return value


async def _parts(parts: list, resolve: Resolve, escape) -> list[tuple[bool, Any]]:
    out = []
    for kind, x in parts:
        if kind == "text":
            out.append((False, x))
            continue
        name, arg_parts = x
        arg = "" if arg_parts is None else "".join(
            str(v) for _, v in await _parts(arg_parts, resolve, None))
        out.append((True, await resolve(name, arg)))
    return out


async def render_str(text: str, resolve: Resolve, escape: Callable[[str], str] | None = None) -> Any:
    parts = _parse(text)
    if len(parts) == 1 and parts[0][0] == "ref":
        (_, value), = await _parts(parts, resolve, escape)
        return value
    out = []
    for is_ref, v in await _parts(parts, resolve, escape):
        if is_ref:
            if v is None:
                raise TemplateError(f"{text!r}: a reference in it is null — refusing to write the word 'None'")
            if isinstance(v, (list, dict)):
                raise TemplateError(f"{text!r}: a reference in it is a {type(v).__name__}; only a value that "
                                    f"is the WHOLE string may be one")
            v = str(v)
            out.append(escape(v) if escape else v)
        else:
            out.append(v)
    return "".join(out)
