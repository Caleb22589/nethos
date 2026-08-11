# Building NETHOS apps

A NETHOS app is a directory with a manifest and an HTML file. There is no build
step, no bundler, no package manager, and no compile. You write a web page, and
`nethos.js` turns it into a program that can drive the operating system.

```
my-app/
  app.json      the manifest
  index.html    your app
  (anything else you want — css, js, images)
```

## Quickest possible start

On the machine:

```bash
nethos-app new notes "Notes"
```

That scaffolds `~/.local/share/nethos/apps/notes`, and it appears in the
launcher immediately — no reload, no restart. Open `index.html` in an editor,
save, and **the running window reloads itself within a second.**

```bash
$EDITOR ~/.local/share/nethos/apps/notes/index.html
nethos-app run notes
```

## The manifest

```json
{
  "id": "notes",
  "name": "Notes",
  "description": "Jot things down",
  "icon": "NO",
  "version": "0.1.0",
  "categories": ["NETHOS"],
  "entry": "index.html",
  "permissions": ["system", "storage"],
  "window": { "width": 820, "height": 600 }
}
```

| Field | Meaning |
| --- | --- |
| `id` | Directory name and API identity. Lowercase, `[a-z0-9._-]`. |
| `icon` | One or two characters shown in the launcher tile. |
| `entry` | Page to open. Defaults to `index.html`. |
| `window` | Size in pixels. Apps open floating and centred. |
| `permissions` | Declared intent — see the honesty note at the bottom. |

## The library

```html
<link rel="stylesheet" href="/lib/nethos.css">
<script src="/lib/nethos.js"></script>
<script>
  (async () => {
    const os = await nethos.ready();
    const status = await os.system.status();
    document.body.textContent = `${status.user}@${status.host}`;
  })();
</script>
```

`nethos.ready()` resolves when the daemon is reachable and returns the SDK.
Every method returns a promise.

### System

```js
await os.system.status()        // host, kernel, uptime, load, mem, battery
await os.system.version()       // build id + live-reload generation
await os.system.notify(text)    // toast on the panel and in every app
await os.system.reload()        // reload every NETHOS surface right now
await os.system.terminal()      // open a terminal
await os.system.lock()          // also: logout(), reboot(), poweroff()
```

### Apps

```js
await os.apps.list()            // NETHOS apps + .desktop entries
await os.apps.listNethos()      // just NETHOS apps
await os.apps.launch("notes")   // launch by id
```

### Windows

These are **real sway windows** — your terminal, your file manager, anything.

```js
const windows = await os.windows.list();
await os.windows.focus(windows[0].id);
await os.windows.close(windows[0].id);
await os.windows.fullscreen(windows[0].id);
await os.windows.closeSelf();
```

### Storage

Persisted to `~/.local/state/nethos/apps/<id>.json` — a real file you can read,
diff and back up. Survives profile wipes, unlike `localStorage`.

```js
await os.storage.set("theme", "dark");
await os.storage.get("theme", "light");   // second arg is the fallback
await os.storage.all();
await os.storage.remove("theme");
await os.storage.clear();
```

### Events

```js
os.on("notify", (m) => os.ui.toast(m.text, m.level));
os.on("reload", () => console.log("something changed on disk"));
os.on("disconnected", () => { /* nethosd is restarting */ });
```

### Hot reload

On by default: when any file under `/usr/share/nethos` or
`~/.local/share/nethos/apps` changes, every open window reloads. If your app
holds unsaved state, opt out and handle it yourself:

```js
os.autoReload(false);
os.on("reload", () => { if (isSaved()) location.reload(); });
```

### UI helpers

```js
os.ui.el("div", "class", "text")   // element factory
os.ui.toast("Saved", "good")       // "info" | "good" | "error"
os.ui.bytes(1048576)               // "1.0 GB"  (takes KB)
os.ui.duration(9000)               // "2h 30m"  (takes seconds)
```

## Styling

`nethos.css` gives you the same design tokens the shell uses, so apps look
native. Use the classes (`neth-card`, `neth-btn`, `neth-table`, `neth-grid`,
`neth-input`, `neth-badge`, `neth-bar`…) or just override the variables:

```css
:root { --neth-accent: #f7a072; }
```

Restyling `/usr/share/nethos/lib/nethos.css` restyles every app at once.

## Where apps live

| Path | Purpose |
| --- | --- |
| `~/.local/share/nethos/apps/<id>` | your apps — wins over the system copy |
| `/usr/share/nethos/apps/<id>` | apps shipped by the repo |

Because user apps take precedence, you can shadow a built-in: copy
`/usr/share/nethos/apps/system` into your user directory and edit it, and
`nethos-update` will never overwrite your version.

## Shipping an app in your OS

Move it into `payload/apps/<id>/` in the repo, commit, push. Every machine gets
it on the next `nethos-update`.

## Two examples ship with NETHOS

- **`system`** — a real dashboard: live stats, a window manager table with
  focus/close, and a launcher grid. Read this one to see most of the SDK used
  in anger.
- **`template`** — the minimal starting point that `nethos-app new` copies.

## An honest note on permissions

`permissions` in the manifest is **declared intent, not a sandbox.** Every app
is served from the same origin and can call the same loopback API, so a
determined app can do anything `nethosd` exposes regardless of what it
declared. Treat installing a NETHOS app exactly like running a shell script as
yourself — because that is what it is. `nethosd` does constrain what the API
can be *asked* to do: it never executes an arbitrary command string, only
`.desktop` ids and app ids that already exist on disk, plus a fixed builtin
table.
