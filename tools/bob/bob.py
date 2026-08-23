"""Bob -- a Python SDK for building NETHOS apps without writing HTML or JS.

A NETHOS app is just app.json + index.html (see payload/apps/template/ for
the hand-written version, and CLAUDE.md for the project layout). Bob lets
you describe that structure and its behaviour with Python calls; `App.build()`
renders it into a real app.json + index.html using the exact same
lib/nethos.css classes and lib/nethos.js / lib/nethos-ui.js SDK every other
NETHOS app already uses -- Bob does not invent a parallel runtime, it just
saves you from typing the HTML/JS by hand.

Bob is deliberately NOT a general Python-to-JS transpiler. Turning arbitrary
Python function bodies into correct JavaScript is a much bigger and riskier
thing to get right than a small desktop project like this needs -- so Bob
does not attempt it. Instead, an app's behaviour (what happens on a click,
on load, on a timer) is built from a small, fixed set of steps -- Toast,
StorageGet, StorageSet, SetText, Fetch, If, ... -- that map one-to-one onto
real methods on nethos.js's `os` object. Anything Bob does not have a step
for yet has an escape hatch: Raw(js_expression) / RawStmt(js_statement)
drops straight to real JavaScript inline. And critically: Bob's output is a
completely normal, readable index.html -- there is no build step at
runtime, nothing to regenerate, and you can keep hand-editing the generated
file afterward exactly the way you would any other NETHOS app. Bob gets you
to a working app fast; it does not lock you into writing Python forever.

Usage:

    import bob

    app = bob.App(
        id="notes", name="Notes", description="A simple note",
        icon="NO", categories=["NETHOS", "Utility"],
        permissions=["storage"], width=760, height=560,
    )
    app.body(
        bob.header("Notes", "edit me and save"),
        bob.card("Your note",
            bob.textarea("note", placeholder="Type something..."),
            bob.row(
                bob.button("Save", id="save", primary=True),
                bob.button("Clear", id="clear"),
                bob.spacer(),
                bob.status_line("saved-at"),
            ),
        ),
    )
    app.actions(
        bob.on_load(
            bob.SetValue("note", bob.StorageGet("note", "")),
        ),
        bob.on_click("save", [
            bob.StorageSet("note", bob.Val("note")),
            bob.Toast("Saved", "good"),
        ]),
        bob.on_click("clear", [
            bob.StorageSet("note", bob.Lit("")),
            bob.SetValue("note", bob.Lit("")),
            bob.Toast("Cleared"),
        ]),
    )
    app.build("payload/apps/notes")

Run this file directly to build every example in examples/:

    python3 tools/bob/bob.py
"""

from __future__ import annotations

import json
import os as _os
import textwrap
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union


# =========================================================================
# HTML elements
#
# Thin builders over the same classes payload/apps/template/index.html uses
# by hand (neth-card, neth-btn, neth-row, ...) -- see docs/DESIGN.md for
# what each one means. Bob does not invent new classes.
# =========================================================================

@dataclass
class El:
    tag: str
    cls: str = ""
    id: str = ""
    text: str = ""
    attrs: dict = field(default_factory=dict)
    children: Sequence["El"] = field(default_factory=list)
    raw_html: Optional[str] = None  # escape hatch: literal HTML, no escaping

    def render(self, indent: int = 2) -> str:
        pad = " " * indent
        if self.raw_html is not None:
            return f"{pad}{self.raw_html}"
        attrs = ""
        if self.cls:
            attrs += f' class="{_esc_attr(self.cls)}"'
        if self.id:
            attrs += f' id="{_esc_attr(self.id)}"'
        for k, v in self.attrs.items():
            attrs += f' {k}="{_esc_attr(str(v))}"'
        if self.children:
            inner = "\n".join(c.render(indent + 2) for c in self.children)
            return f"{pad}<{self.tag}{attrs}>\n{inner}\n{pad}</{self.tag}>"
        body = _esc_text(self.text)
        return f"{pad}<{self.tag}{attrs}>{body}</{self.tag}>"


def _esc_text(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _esc_attr(s: str) -> str:
    return _esc_text(s).replace('"', "&quot;")


def raw(html: str) -> El:
    """Escape hatch: drop literal HTML in verbatim, no escaping applied."""
    return El(tag="", raw_html=html)


def header(title: str, subtitle: Optional[str] = None) -> El:
    kids = [El("h1", text=title)]
    if subtitle:
        kids.append(El("span", cls="neth-sub", text=subtitle))
    return El("div", cls="neth-app-header", children=kids)


def card(title: Optional[str], *children: El) -> El:
    kids = []
    if title:
        kids.append(El("div", cls="neth-card-title", text=title))
    kids.extend(children)
    return El("div", cls="neth-card", children=kids)


def text(s: str, dim: bool = False, mono: bool = False, id: str = "") -> El:
    cls = " ".join(c for c in ("neth-dim" if dim else "", "neth-mono" if mono else "") if c)
    return El("p" if not id else "span", cls=cls, id=id, text=s)


def label(s: str, for_: str) -> El:
    return El("label", cls="neth-label", attrs={"for": for_}, text=s)


def textarea(id: str, placeholder: str = "") -> El:
    return El("textarea", cls="neth-textarea", id=id,
               attrs={"placeholder": placeholder} if placeholder else {})


def input_(id: str, placeholder: str = "", input_type: str = "text") -> El:
    attrs = {"type": input_type}
    if placeholder:
        attrs["placeholder"] = placeholder
    return El("input", cls="neth-input", id=id, attrs=attrs)


def button(text: str, id: str, primary: bool = False) -> El:
    cls = "neth-btn neth-btn-primary" if primary else "neth-btn"
    return El("button", cls=cls, id=id, text=text)


def row(*children: El) -> El:
    return El("div", cls="neth-row", children=list(children))


def spacer() -> El:
    return El("span", cls="neth-spacer")


def status_line(id: str, initial: str = "...") -> El:
    return El("span", cls="neth-dim neth-mono", id=id, text=initial)


def div(cls: str = "", *children: El) -> El:
    return El("div", cls=cls, children=list(children))


# =========================================================================
# JS-side expressions and statements
#
# Not a Python-source transpiler -- a small, fixed vocabulary of steps built
# via Python calls, each of which knows how to render itself as real
# JavaScript that calls the real nethos.js `os` object. Raw()/RawStmt() are
# the escape hatch for anything not covered.
# =========================================================================

class Expr:
    def js(self) -> str:
        raise NotImplementedError


@dataclass
class Lit(Expr):
    """A literal value -- string, number, bool, list or dict -- encoded as
    JSON, which is also valid JavaScript for every type Bob supports."""
    value: object

    def js(self) -> str:
        return json.dumps(self.value)


@dataclass
class Raw(Expr):
    """Escape hatch: literal JavaScript, inserted verbatim."""
    code: str

    def js(self) -> str:
        return self.code


@dataclass
class Val(Expr):
    """The current .value of an <input>/<textarea> by element id."""
    element_id: str

    def js(self) -> str:
        return f'document.getElementById({json.dumps(self.element_id)}).value'


@dataclass
class TextOf(Expr):
    """The current .textContent of an element by id."""
    element_id: str

    def js(self) -> str:
        return f'document.getElementById({json.dumps(self.element_id)}).textContent'


@dataclass
class StorageGet(Expr):
    """await os.storage.get(key, fallback) -- this app's own persisted state,
    see nethos.js's storage docs (~/.local/state/nethos/apps/<id>.json)."""
    key: str
    default: object = None

    def js(self) -> str:
        return f'await os.storage.get({json.dumps(self.key)}, {json.dumps(self.default)})'


@dataclass
class SystemStatus(Expr):
    """await os.system.status() -- host/user/load/mem/uptime, see nethosd's
    /api/status."""

    def js(self) -> str:
        return "await os.system.status()"


@dataclass
class Prop(Expr):
    """Attribute access on another expression's result, e.g.
    Prop(SystemStatus(), "host") -> (await os.system.status()).host"""
    base: Expr
    path: str

    def js(self) -> str:
        return f"({self.base.js()}).{self.path}"


@dataclass
class Now(Expr):
    """The current time as a locale-formatted string, e.g. for a
    'saved at ...' label."""

    def js(self) -> str:
        return "new Date().toLocaleString()"


class Fmt(Expr):
    """A JS template literal: Fmt("{host} up {up}", host=..., up=...) ->
    `${...} up ${...}`. Plain text between placeholders is copied verbatim."""

    def __init__(self, template: str, **parts: Expr):
        self.template = template
        self.parts = parts

    def js(self) -> str:
        out = self.template
        for name, expr in self.parts.items():
            placeholder = "{" + name + "}"
            out = out.replace(placeholder, "${" + expr.js() + "}")
        return "`" + out.replace("`", "\\`") + "`"


class Stmt:
    def js(self, indent: str = "") -> list:
        raise NotImplementedError


@dataclass
class RawStmt(Stmt):
    """Escape hatch: one or more literal JavaScript statements, inserted
    verbatim."""
    code: str

    def js(self, indent: str = "") -> list:
        return [indent + line for line in self.code.splitlines()]


@dataclass
class SetText(Stmt):
    element_id: str
    expr: Expr

    def js(self, indent: str = "") -> list:
        target = f'document.getElementById({json.dumps(self.element_id)}).textContent'
        return [f"{indent}{target} = {self.expr.js()};"]


@dataclass
class SetValue(Stmt):
    element_id: str
    expr: Expr

    def js(self, indent: str = "") -> list:
        target = f'document.getElementById({json.dumps(self.element_id)}).value'
        return [f"{indent}{target} = {self.expr.js()};"]


@dataclass
class StorageSet(Stmt):
    """await os.storage.set(key, value)."""
    key: str
    expr: Expr

    def js(self, indent: str = "") -> list:
        return [f"{indent}await os.storage.set({json.dumps(self.key)}, {self.expr.js()});"]


@dataclass
class Toast(Stmt):
    """os.ui.toast(message, level) -- a transient in-window notification.
    level is "info" (default), "good" or "bad"."""
    message: Union[str, Expr]
    level: Optional[str] = None

    def js(self, indent: str = "") -> list:
        msg = self.message.js() if isinstance(self.message, Expr) else json.dumps(self.message)
        level = json.dumps(self.level) if self.level else "undefined"
        return [f"{indent}os.ui.toast({msg}, {level});"]


@dataclass
class Notify(Stmt):
    """os.system.notify(text, level) -- a real system notification, not
    just an in-window toast."""
    message: Union[str, Expr]
    level: Optional[str] = None

    def js(self, indent: str = "") -> list:
        msg = self.message.js() if isinstance(self.message, Expr) else json.dumps(self.message)
        level = json.dumps(self.level) if self.level else "undefined"
        return [f"{indent}await os.system.notify({msg}, {level});"]


@dataclass
class Fetch(Stmt):
    """Escape hatch onto nethosd's own HTTP API directly (see
    payload/nethosd/nethosd.py's routes) for anything nethos.js does not
    already wrap. Stores the parsed JSON response in a local variable,
    usable by name in later Raw()/RawStmt() steps in the same action."""
    var: str
    method: str
    path: str
    body: Optional[dict] = None

    def js(self, indent: str = "") -> list:
        opts = f'{{method: {json.dumps(self.method)}'
        if self.body is not None:
            opts += f', headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({json.dumps(self.body)})'
        opts += "}"
        return [
            f"{indent}const {self.var} = await (await fetch("
            f'"http://127.0.0.1:7777" + {json.dumps(self.path)}, {opts})).json();',
        ]


@dataclass
class Var(Expr):
    """A reference to a name bound earlier in the same action by Let() --
    use this instead of repeating an expensive expression like
    SystemStatus() so it only actually runs once per action."""
    name: str

    def js(self) -> str:
        return self.name


@dataclass
class Let(Stmt):
    """const <name> = <expr>; -- bind a value once, then reference it by
    name (via Var(name)) in later steps of the same action instead of
    re-evaluating the expression (and re-fetching, for anything backed by
    an HTTP call like SystemStatus()) every time."""
    name: str
    expr: Expr

    def js(self, indent: str = "") -> list:
        return [f"{indent}const {self.name} = {self.expr.js()};"]


@dataclass
class If(Stmt):
    cond: Expr
    then: Sequence[Stmt]
    else_: Sequence[Stmt] = field(default_factory=list)

    def js(self, indent: str = "") -> list:
        out = [f"{indent}if ({self.cond.js()}) {{"]
        for s in self.then:
            out.extend(s.js(indent + "  "))
        if self.else_:
            out.append(f"{indent}}} else {{")
            for s in self.else_:
                out.extend(s.js(indent + "  "))
        out.append(f"{indent}}}")
        return out


# =========================================================================
# Actions -- where a list of Stmts gets wired to a real event
# =========================================================================

@dataclass
class Action:
    kind: str          # "load" | "click" | "interval"
    target: Optional[str]   # element id for "click"; ms for "interval"
    steps: Sequence[Stmt]


def on_load(*steps: Stmt) -> Action:
    """Runs once, right after `await nethos.ready()`."""
    return Action("load", None, list(steps))


def on_click(element_id: str, steps: Sequence[Stmt]) -> Action:
    return Action("click", element_id, steps)


def every(ms: int, *steps: Stmt) -> Action:
    """Runs immediately, then again every `ms` milliseconds -- the same
    pattern payload/apps/template/index.html uses for its status tick."""
    return Action("interval", str(ms), list(steps))


# =========================================================================
# App
# =========================================================================

class App:
    def __init__(self, id: str, name: str, description: str = "", icon: str = "",
                 version: str = "1.0.0", categories: Optional[Sequence[str]] = None,
                 permissions: Optional[Sequence[str]] = None,
                 mode: str = "window", width: int = 760, height: int = 560,
                 position: str = "top-right"):
        self.id = id
        self.name = name
        self.description = description
        self.icon = icon or (name[:2].upper() if name else "AP")
        self.version = version
        self.categories = list(categories) if categories else ["NETHOS"]
        self.permissions = list(permissions) if permissions else []
        self.mode = mode
        self.width = width
        self.height = height
        self.position = position
        self._body: Sequence[El] = []
        self._actions: Sequence[Action] = []

    def body(self, *elements: El) -> "App":
        self._body = elements
        return self

    def actions(self, *actions: Action) -> "App":
        self._actions = actions
        return self

    # ---- rendering -------------------------------------------------

    def _app_json(self) -> str:
        data = {
            "id": self.id, "name": self.name, "description": self.description,
            "icon": self.icon, "version": self.version, "categories": self.categories,
            "entry": "index.html", "permissions": self.permissions,
            "mode": self.mode, "position": self.position, "floating": False,
            "width": self.width, "height": self.height,
        }
        return json.dumps(data, indent=2) + "\n"

    def _action_js(self, a: Action) -> str:
        lines = [s_line for s in a.steps for s_line in s.js("    ")]
        return "\n".join(lines)

    def _script(self) -> str:
        parts = ["(async () => {", "  const os = await nethos.ready();"]
        for a in self._actions:
            if a.kind == "load":
                parts.append("  {")
                parts.append(self._action_js(a) or "    // (nothing to do)")
                parts.append("  }")
        for a in self._actions:
            if a.kind == "click":
                parts.append(
                    f'  document.getElementById({json.dumps(a.target)})'
                    f'.addEventListener("click", async () => {{')
                parts.append(self._action_js(a) or "    // (nothing to do)")
                parts.append("  });")
        for a in self._actions:
            if a.kind == "interval":
                parts.append("  async function _tick_" + a.target + "() {")
                parts.append(self._action_js(a) or "    // (nothing to do)")
                parts.append("  }")
                parts.append(f"  _tick_{a.target}();")
                parts.append(f"  setInterval(_tick_{a.target}, {a.target});")
        parts.append("})();")
        return "\n".join(parts)

    _TEMPLATE = textwrap.dedent("""\
        <!DOCTYPE html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>{title} — NETHOS</title>
          <link rel="stylesheet" href="/lib/nethos.css">
          <script src="/lib/nethos.js"></script>
          <script src="/lib/nethos-ui.js"></script>
        </head>
        <body>
        {body}

        <script>
        {script}
        </script>
        </body>
        </html>
        """)

    def _html(self) -> str:
        # .format() after dedenting the skeleton, not an f-string dedented
        # afterward -- the substituted body/script each carry their own
        # indentation, and dedent() looks at every line in the *final*
        # string, so doing it the other way around let a single
        # zero-indent generated line (e.g. the top of the JS IIFE) defeat
        # dedenting the whole template.
        body_html = "\n".join(e.render(2) for e in self._body)
        return self._TEMPLATE.format(
            title=_esc_text(self.name), body=body_html, script=self._script())

    def build(self, out_dir: str) -> None:
        """Write app.json + index.html into out_dir (created if missing).

        The output is a normal NETHOS app -- see payload/apps/template/ for
        what a hand-written one looks like. This is the last step Bob is
        involved in: nothing at runtime depends on Bob, and the generated
        index.html is meant to be edited by hand afterward like any other
        app in payload/apps/.
        """
        _os.makedirs(out_dir, exist_ok=True)
        with open(_os.path.join(out_dir, "app.json"), "w") as f:
            f.write(self._app_json())
        with open(_os.path.join(out_dir, "index.html"), "w") as f:
            f.write(self._html())
        print(f"bob: built {self.id} -> {out_dir}/app.json, {out_dir}/index.html")


def _build_examples() -> None:
    import glob
    import importlib.util

    here = _os.path.dirname(_os.path.abspath(__file__))
    for path in sorted(glob.glob(_os.path.join(here, "examples", "*.py"))):
        spec = importlib.util.spec_from_file_location("bob_example", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # each example builds itself on import


if __name__ == "__main__":
    _build_examples()
