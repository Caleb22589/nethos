# How NETHOS works, and the bugs that shaped it

Written while it was fresh. The bug list is not decoration: every entry below
cost hours, and each one is a trap the next person would otherwise fall into.

---

## The package manager

### Reading packages

Three formats, parsed directly from their specifications:

| Format | Container | Metadata |
| --- | --- | --- |
| `.deb` | `ar` archive | `control.tar` — RFC822 |
| `.pkg.tar.zst` | tar | `.PKGINFO` — `key = value` |
| `.rpm` | lead + headers + cpio | binary index into a data blob |

Nothing shells out to `dpkg`, `rpm` or `alien`. The system has to read its own
packages before any of those exist on it, and depending on them would put
another distribution's tooling between NETHOS and its own format.

Two details that are easy to get wrong and cost a day each:

- **RPM header padding.** The *signature* header is padded to an 8-byte
  boundary; the main header is not — the payload starts the byte after it.
  Padding both walks into the payload, and the failure surfaces as
  "unsupported compression format" from whatever decompressor you hand it to.
- **`tarfile.extractall(filter=...)`** arrived in Python 3.12. The builder runs
  Debian's 3.11, so there is a shim that validates paths itself.

### The capability index

Package names differ between distributions; sonames do not. At install time
npkg reads the ELF `DT_SONAME` of every library and records it in
`/var/lib/npkg/capabilities.json`. Requirements resolve against installed
package names, `Provides`, **sonames**, and **file paths**, in that order.

RPM benefits most, because Fedora expresses nearly everything as capabilities:

```
Requires: libc.so.6(GLIBC_2.34)(64bit)
Requires: /usr/bin/sh
```

`libc.so.6(GLIBC_2.34)(64bit)` is normalised to `libc.so.6` and kept as a real
requirement. The symbol version is dropped deliberately: we index sonames, not
symbol versions, and asserting a version we cannot verify would be worse than
not asserting it. Only `rpmlib()` and `config()` are discarded — those describe
the packaging system, not the machine.

### The Arch relayout

Debian's multiarch paths are flattened (`/usr/lib/x86_64-linux-gnu` →
`/usr/lib`), `sbin` merges into `bin`, and `/bin`, `/lib`, `/lib64`, `/sbin`,
`/usr/sbin` become compatibility symlinks.

Two traps, both of which produce an unbootable system:

- **Symlink targets need rewriting too**, not just paths. A link pointing at
  `/usr/lib/x86_64-linux-gnu/ld-linux.so` has to be rewritten or it dangles.
- **A real file beats a symlink on collision.** The loader and its symlink
  collapse onto one path during relayout; keeping the symlink leaves it
  pointing at itself, and the result is `chroot: Too many levels of symbolic
  links`.

---

## The desktop

```
sway (wlroots)
  └── nethos-view ......... one GTK4 process hosting every surface
        ├── panel.html         layer-shell, top, exclusive zone
        ├── dock.html          layer-shell, top, bottom edge
        ├── menu.html          layer-shell, overlay, hidden until asked
        └── desktop.html       layer-shell, bottom (above swaybg)
  └── nethosd ............. Python HTTP API on 127.0.0.1:7777
        ├── sway IPC           windows, workspaces, commands
        ├── /api/events        one server-sent event stream
        └── /api/log           where the surfaces report in
```

The pages talk to the host through `window.nethosHost`:

```js
nethosHost.exclusive(48)        // reserve space (dock pinning)
nethosHost.inputRect(x,y,w,h)   // limit the clickable region
nethosHost.hide() / .show()
```

### gtk4-layer-shell needs LD_PRELOAD

GTK4 through a language binding gives no link-time opportunity to load it
first, so the library documents `LD_PRELOAD` as the supported route. Without
it `init_for_window()` silently does nothing and every panel becomes an
ordinary window — no error, just wrong behaviour.

The runtime package ships `libgtk4-layer-shell.so.0`; the bare `.so` belongs to
the `-dev` package and is not installed. Testing only for the bare name meant
the preload was skipped without a word.

### Events come from outside the browser

**This is the single most important thing in the file.**

WebKit runs *one* network process for all surfaces, so the ~6 connections a
browser allows per host are shared across the entire shell. `/api/events` never
returns. With one stream per surface the pool was full before the shell fetched
anything, and every request after the first handful queued inside the browser
and was never sent.

On screen that looked like: a clock frozen at the minute it loaded, widgets
that never updated, and buttons that did nothing — while `nethosd` answered
normally on its socket and `Super+D` still opened the launcher. Two theories
fit those symptoms perfectly and neither was right:

- **WebKit suspending hidden pages.** Plausible: layer-shell surfaces never
  take focus. Wrong.
- **Broken GPU compositing.** Also real — `chromium --disable-gpu` renders
  where plain `chromium` draws nothing on this hardware — but not this bug.

What settled it was the Web Inspector's Network tab showing requests pending
forever, next to `nethosd`'s request log showing those requests never
arriving. Client-side queue, proven in one look.

So `nethos-view` holds a single stream in a plain Python thread — outside the
browser, uncounted, unthrottled — and forwards events with
`window.nethosEvent()`. The pages open no connections of their own.

**If you add a feature to the shell, do not open an `EventSource`.** Use
`onEvent()`, which is delivered by the host.

### Transparency, and a boxed type that lies

```python
Gdk.RGBA(red=0, green=0, blue=0, alpha=0)     # does nothing
```

GdkRGBA is a boxed type and PyGObject ignores constructor keywords for boxed
types — it warns in a `DeprecationWarning` and carries on. The struct arrives
uninitialised, alpha is not 0, WebKit paints every surface opaque, and the
desktop is a full-screen white sheet with the shell rendering invisibly
underneath. Assign the fields after construction.

### Two full-screen surfaces on one layer

`swaybg` draws on the `background` layer, and the desktop used to as well.
Surfaces on the same layer stack by creation order, and `exec_always`
re-creates swaybg's on every reload — putting it over the desktop, where it
silently ate every click aimed at a widget. The desktop lives on `bottom` now.

---

## Ten things Debian does in maintainer scripts

npkg never runs `postinst`, so installing a package cannot execute arbitrary
code as root. The cost is that everything a postinst would have done has to be
done deliberately. Each of these first appeared as an unrelated-looking bug:

| Missing step | How it presented |
| --- | --- |
| `pam-auth-update` | `PAM Failure, aborting` — nobody can log in, including root |
| `update-alternatives` | `vim: command not found` after installing vim |
| `depmod` | `ALERT! UUID=… does not exist` — no root filesystem |
| `systemctl preset` | boots to a serial console, no desktop |
| `glib-compile-schemas` | GTK apps abort: "No GSettings schemas are installed" |
| `update-ca-certificates` | "System trust contains zero trusted certificates" |
| `fc-cache` | every GUI app rebuilds the font cache on first launch |
| `ssh-keygen -A` + `sshd` user | sshd refuses to start |
| `sshd_config` install | Debian ships the template in `/usr/share/openssh/` |
| `gio-querymodules`, `update-desktop-database`, `update-mime-database` | missing caches, paid for at runtime |

Two of those needed a package nobody would guess at: `update-ca-certificates`
is a shell script that shells out to **openssl**, which `ca-certificates` only
*Recommends*; and it `cd`s into `/etc/ssl/certs`, a directory the postinst
creates and the `.deb` does not ship.

**The right fix, still unwritten:** a trigger system in npkg that runs these
after a transaction, so installing a package later rebuilds the caches it
invalidates. Today they are hardcoded in the image build, which does nothing
for packages installed afterwards with `npkg fetch`.

---

## Build and boot

The build runs inside a throwaway Debian VM, because it must run as root:
setuid bits survive a tarball but file *ownership* does not, and a `sudo` owned
by anyone but root refuses to run. macOS cannot mount ext4 either.

```
Debian builder VM (root)
  ├── partitions the target: ESP + ext4 root
  ├── npkg-bootstrap: resolve, download, convert, install
  ├── chroot: initramfs, machine-id, fstab, caches, GRUB
  └── powers off
```

Build-system traps, all of which hid failures rather than causing them:

- **`set -o pipefail` + `ls` on an empty glob** aborts with no message: `ls`
  exits 2 when a glob matches nothing and the pipeline inherits that rather
  than `wc -l`'s zero. Use `find`.
- **A failed build must power its VM down.** Otherwise it sits at a login
  prompt holding write locks, and the *next* build dies instantly with
  `Failed to get "write" lock` — which reads as a corrupt image.
- **QEMU exiting does not mean the build worked.** The host script printed
  `Built:` over a failure for weeks. It now requires the guest's completion
  marker.
- **Downloads must be checked against the archive's stated `Size`.** A
  connection closed early gives a short file and no exception; then "does the
  file exist" keeps it forever, and the package is silently missing from every
  image built from that cache.

### Virtualisation

- `virtio-vga` starts in VGA 640x480 and never modesets past it — the shell
  lays itself out for a quarter-size screen and the dock renders as overlapping
  garbage. Use `virtio-gpu-pci` / `virtio-gpu-gl-pci`, which have no VGA
  compatibility layer.
- KVM falls back to TCG silently when `/dev/kvm` is unwritable *or* when
  virtualisation is off in firmware. Roughly 20× slower, with no warning unless
  you add one.
- `NETHOS_QMP=/tmp/nethos-qmp.sock scripts/run.sh` opens a QMP socket:
  screenshots and injected keyboard/pointer events with nothing running inside
  the guest. Invaluable when the desktop is the thing that is broken.

### Real hardware

- Firmware matters. virtio has no firmware blobs; real AMD and Intel graphics
  load firmware at probe time or fail to modeset, and most wifi does the same.
  Those packages live in Debian's `non-free-firmware` component, which the
  archive reader now reads alongside `main`.
- GPU compositing is **off by default**. On the hardware this was developed
  against, `chromium` drew a blank window and logged "Frame latency is
  negative" while `chromium --disable-gpu` rendered fine. WebKit fails the same
  way. `NETHOS_GPU_COMPOSITING=1` re-enables it.
- Secure Boot must be off: this GRUB is not signed.

---

## Debugging it

```bash
nethos-doctor                      # start here, always
tail -f ~/.cache/nethos/nethosd.log
tail -f ~/.cache/sway.log          # the compositor, and every console.log
```

`nethos-view` enables `write_console_messages_to_stdout`, so **`console.log`
from any shell page lands in `~/.cache/sway.log`**. Injecting a probe into
`/usr/share/nethos/shell/shell.js` and running `nethos-reload` is the fastest
way to see what the UI is really doing — that is how the click path was
finally proven.

`Super+Return` opens a terminal through sway without touching the API, which
is the way in when the shell itself is frozen.

**Measure before concluding.** Every wrong turn in this project came from a
theory that fit the symptoms. The frame-clock bug, the connection-pool
exhaustion, the swaybg layer collision and the resolution mismatch were each
found by an instrument, not an argument.
