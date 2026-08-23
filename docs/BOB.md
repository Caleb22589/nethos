# Bob — building NETHOS apps in Python

`tools/bob/` is a small Python library for people who want to build a NETHOS
app (docs/APPS.md) without writing HTML or JavaScript by hand.

## What it is, and what it deliberately is not

A NETHOS app is just `app.json` + `index.html`, served by `nethosd` and
loaded into a WebView — see `payload/apps/template/` for a hand-written one.
Bob lets you describe that structure and its behaviour with Python calls;
`App.build()` renders real `app.json` + `index.html` from it, using the exact
same `lib/nethos.css` classes and `lib/nethos.js` / `lib/nethos-ui.js` SDK
every other app already uses. Bob is not a second runtime — the app it
produces has no dependency on Bob at all once built.

**Bob is not a general Python-to-JS transpiler.** Turning arbitrary Python
function bodies into correct JavaScript is a much bigger and riskier thing to
get right than this project needs, and it would work against the point of
the OS: every surface is a plain, inspectable web page (see `docs/HANDOFF.md`
— `console.log` and a reload is the whole debugging story here). So an app's
behaviour — what happens on a click, on load, on a timer — is built from a
small, fixed set of steps (`Toast`, `StorageGet`, `SetText`, `Fetch`, `If`,
...) that map one-to-one onto real methods on `nethos.js`'s `os` object.
Anything Bob doesn't have a step for yet has an escape hatch: `Raw(js_expr)`
/ `RawStmt(js_code)` drops straight to real JavaScript inline. And critically
— the generated `index.html` is a completely normal file. Nothing regenerates
it at runtime; you can keep hand-editing it afterward exactly the way you
would any other app in `payload/apps/`. Bob gets you to a working app fast;
it does not lock you into writing Python forever.

## Using it

```python
import sys
sys.path.insert(0, "tools/bob")
import bob

app = bob.App(
    id="notes", name="Notes", description="A simple note",
    icon="NO", categories=["NETHOS", "Utility"],
    permissions=["storage"], width=760, height=560,
)
app.body(
    bob.header("Notes", "edit me and save"),
    bob.card("Your note",
        bob.textarea("note", placeholder="Type something…"),
        bob.row(
            bob.button("Save", id="save", primary=True),
            bob.button("Clear", id="clear"),
        ),
    ),
)
app.actions(
    bob.on_load(bob.SetValue("note", bob.StorageGet("note", ""))),
    bob.on_click("save", [
        bob.StorageSet("note", bob.Val("note")),
        bob.Toast("Saved", "good"),
    ]),
    bob.on_click("clear", [
        bob.StorageSet("note", bob.Lit("")),
        bob.SetValue("note", bob.Lit("")),
    ]),
)
app.build("payload/apps/notes")
```

See `tools/bob/examples/notes_app.py` for the full, working version of the
example above (it round-trips `payload/apps/template/`'s own Save/Clear/tick
behaviour) — run `python3 tools/bob/bob.py` to rebuild every example. Verified
interactively against `tools/mock_nethosd.py` (real Save/Clear/reload
persistence, real live system-status ticking, real toasts) before landing.

## Reference

**Elements** (`bob.py`, mirroring `lib/nethos.css`): `header`, `card`, `text`,
`label`, `textarea`, `input_`, `button`, `row`, `spacer`, `status_line`,
`div`, `raw` (literal HTML escape hatch).

**Expressions** (values on the JS side): `Lit` (a JSON literal), `Val`
(an element's current `.value`), `TextOf` (an element's `.textContent`),
`StorageGet`, `SystemStatus`, `Prop` (attribute access, e.g.
`Prop(SystemStatus(), "host")`), `Now`, `Fmt` (a template string —
`Fmt("{a} of {b}", a=..., b=...)`), `Var` (reference a name bound by `Let`),
`Raw` (literal JS expression).

**Statements** (what an action does): `SetText`, `SetValue`, `StorageSet`,
`Toast`, `Notify` (a real system notification, not just an in-window toast),
`Fetch` (escape hatch onto any `nethosd` route — see
`payload/nethosd/nethosd.py`'s routes — not already wrapped by `nethos.js`),
`Let` (bind a value once so an expensive expression like `SystemStatus()`
doesn't get re-evaluated for every place you use it), `If`, `RawStmt`
(literal JS statements).

**Actions**: `on_load(*steps)` (once, after `nethos.ready()`),
`on_click(element_id, steps)`, `every(ms, *steps)` (immediately, then every
`ms` milliseconds — the same pattern `template`'s own status tick uses).

## What's missing

No `for`/list-rendering step yet (building a dynamic list — e.g. Files'
directory listing — currently needs `Fetch` + `RawStmt` for the render part).
No `ui.ask`/`ui.confirm`/`ui.menu` bindings yet, even though every app is
supposed to use those instead of `prompt()`/`confirm()`/`alert()` (CLAUDE.md)
— for now, reach for `RawStmt` to call `nethos-ui.js`'s `ui.*` directly.
