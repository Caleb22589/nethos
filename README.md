# NETHOS

A custom Arch Linux distribution whose desktop environment is written in HTML,
CSS and JavaScript and rendered by Chromium — running as a real Wayland desktop,
not a kiosk.

## The idea

NETHOS is **hybrid**: sway (a wlroots compositor) does the actual window
management, while the visible desktop — the panel, the taskbar, the application
launcher — is a web page. Native Linux applications open as ordinary managed
windows, and the web shell can see and control them.

The piece that makes this work is `nethosd`, a small stdlib-only Python daemon:

```
  Chromium (--app, app_id=nethos-panel)   <- the desktop you see
        |  fetch() over 127.0.0.1:7777
        v
  nethosd                                 <- the bridge
        |  swaymsg / .desktop files / /proc
        v
  sway + the real system
```

The shell is just a web page, so **you customise the desktop by editing HTML and
CSS**. No recompiling, no widget toolkit.

### Why a daemon instead of letting the page do it

A web page cannot list installed applications, spawn processes, or move windows.
`nethosd` provides exactly those operations and nothing more. It deliberately
**never executes an arbitrary command string from the page** — a launch request
names a `.desktop` id that must already exist on disk, or one of a fixed table of
builtins (`poweroff`, `reboot`, `logout`, `lock`, `terminal`, `menu-toggle`). It
binds loopback only. So a hostile page inside the shell can do no more than a
user clicking through the application menu.

## This repository is the operating system

`payload/` is not a copy of the system — it *is* the system. A machine running
NETHOS tracks this repo and applies it with `nethos-update`, so shipping a
change to your OS is a `git push`.

```
nethos/
  scripts/
    build.sh              build the disk + seed ISO on macOS
    run.sh                boot the VM under QEMU
  cloud-init/             first-boot provisioning
  payload/                >>> THE OPERATING SYSTEM <<<
    install-nethos.sh     stock Arch -> NETHOS; --files-only for updates
    nethosd/nethosd.py    the bridge: API, app host, live-reload engine
    shell/                THE DESKTOP: panel.html, menu.html, style.css, shell.js
    lib/nethos.js         THE APP SDK
    lib/nethos.css        the design system apps build on
    apps/system/          example app: live dashboard using most of the SDK
    apps/template/        the starting point `nethos-app new` copies
    bin/nethos-app        create / list / run apps
    bin/nethos-reload     apply changes to a running system, no reboot
    bin/nethos-update     pull this repo and apply it
    bin/nethos-session    starts the shell inside sway
    systemd/nethosd.service
    sway/config           compositor rules
  docs/
    APPS.md               how to build apps  <- start here
    UPDATING.md           how updates and hot reload work
  build/                  images (large, gitignored)
```

## Building on top

```bash
nethos-app new notes "Notes"        # scaffold; appears in the launcher at once
$EDITOR ~/.local/share/nethos/apps/notes/index.html
```

Save the file and the running window reloads itself in under a second. An app
is a web page plus a manifest:

```html
<link rel="stylesheet" href="/lib/nethos.css">
<script src="/lib/nethos.js"></script>
<script>
  const os = await nethos.ready();
  await os.system.notify("hello from my app");
  const windows = await os.windows.list();   // real sway windows
  await os.storage.set("count", 1);          // real file on disk
</script>
```

Full reference: **[docs/APPS.md](docs/APPS.md)**.

## Changing the system without rebooting

| You changed | Command | Cost |
| --- | --- | --- |
| Shell, an app, the CSS | *nothing — just save* | < 1s, automatic |
| Force it now | `nethos-reload` | instant |
| `nethosd` / the API | `nethos-reload --daemon` | ~1s |
| sway config | `swaymsg reload` | instant |
| Packages, branding | `nethos-update --packages` | minutes |

`nethosd` watches the served tree, bumps a generation counter, and pushes an
event over SSE; every open surface is subscribed and reloads itself. Details in
**[docs/UPDATING.md](docs/UPDATING.md)**.

## Updating from this repository

```bash
nethos-update              # fetch, install, restart daemon, reload windows
nethos-update --status     # installed vs available
nethos-update --ref v2     # any branch, tag or commit — also how you roll back
```

Point `/etc/nethos/update.conf` at your own fork and your machines track you.

## Building and running

```bash
cd nethos
./scripts/build.sh
./scripts/run.sh
```

`build.sh` copies the **official Arch Linux x86_64 cloud image** into a 40 GB
working disk and bakes a cloud-init seed ISO carrying the payload. `run.sh` boots
it with a GUI window plus the serial console in your terminal.

First boot runs the installer automatically (`nethos-install.service`), then
reboots straight into the NETHOS desktop. Watch it with:

```bash
./scripts/run.sh --console
```

Log in as **`neth`** / **`nethos`** (root password is also `nethos`). Change both —
they are throwaway VM credentials, not a security posture.

### A word on speed

This host is Apple Silicon and the guest is x86_64, so QEMU runs in **full TCG
emulation** — there is no hardware acceleration for that pairing, and there
cannot be. The first-boot install (a full `pacman -Syu` plus Chromium) is the
worst of it and takes a long while. Once installed, the desktop is usable but
noticeably slow; Chromium is doing software rendering on an emulated CPU.

If responsiveness ever matters more than being byte-for-byte official Arch, the
same payload runs on Arch Linux ARM at near-native speed under Apple's hypervisor.

## Keys

| Key | Action |
| --- | --- |
| `Super`+`D` / `Super`+`Space` | Toggle the launcher |
| `Super`+`Return` | Terminal (foot) |
| `Super`+`Q` | Close window |
| `Super`+`Arrows` | Move focus |
| `Super`+`Shift`+`Arrows` | Move window |
| `Super`+`F` | Fullscreen |
| `Super`+`1..4` | Switch workspace |
| `Super`+`R` | Resize mode |

## Customising the desktop

Everything visible lives in `/usr/share/nethos/shell/` on the guest.

```bash
sudo nano /usr/share/nethos/shell/style.css   # then re-open the panel
swaymsg '[app_id="nethos-panel"] kill' && nethos-session &
```

The colour system is the `:root` block at the top of `style.css`; the sway
config reuses the same hex values for window borders so the two read as one
system.

### nethosd API

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/apps` | installed applications (parsed `.desktop`) |
| `GET` | `/api/windows` | live sway windows |
| `GET` | `/api/status` | clock, load, memory, battery |
| `GET` | `/api/menu` | is the launcher open |
| `POST` | `/api/launch` | `{"id": "foot.desktop"}` or `{"builtin": "reboot"}` |
| `POST` | `/api/window` | `{"action": "focus\|close\|fullscreen", "id": 12}` |
| `POST` | `/api/menu` | `{"open": true\|false}` |

Adding a widget to the panel is: add an endpoint to `nethosd.py`, then render it
in `shell.js`.

## Notes from bringing this up

Four things bit during the first build. They are all fixed in the payload, but
they are the kind of thing that will bite again if you change that code:

1. **`install -d -o user a/b` only chowns the leaf.** A root-owned `~/.config`
   left behind by the installer makes Chromium fail to resolve its crashpad
   database path and abort at startup with `SIGTRAP` — a spectacularly
   misleading symptom for a permissions bug. The installer now creates each
   level explicitly and chowns the home directory at the end.
2. **Chromium ignores `--class` in `--app` mode.** It derives its own app_id
   from the URL (`chrome-127.0.0.1__panel.html-Default`), so the sway rules and
   `nethosd` both key off the page name instead.
3. **A Wayland client sizes itself.** `resize set` from sway is undone the
   moment Chromium reasserts its own size, so the panel and launcher pass
   `--window-size` instead. Position still comes from sway, and must be `move
   absolute position` — plain `move position` is workspace-relative and lands
   below the reserved top gap.
4. **Don't route an argv through `swaymsg exec`.** It gets flattened to a
   string and re-split by `sh`, which silently dropped every argument after
   `chromium`. `nethosd` spawns processes directly instead; it is started by
   sway, so it already has the session environment.

`grim` and `epiphany` are also present in the running VM — they were installed
while diagnosing the above (`grim` takes the screenshots). Neither is part of
the distro definition, so a clean rebuild will not include them.

## Developing the shell without booting the VM

The shell is ordinary web code, so it can be driven against a fake backend on
your Mac — far faster than iterating inside an emulated VM:

```bash
python3 tools/mock_nethosd.py payload/shell
```

Then open <http://127.0.0.1:7777/panel.html> or `/menu.html`.
