# NETHOS — state of the project

Written to hand the work to someone (or something) with no history of it.

## What this is

A Linux distribution built from a plain Debian package archive with a Python
package manager, running a desktop that is HTML/CSS rendered by WebKit.

- Repo: https://github.com/Caleb22589/nethos (public)
- Working dir: `/Users/mac/Downloads/untitled folder/nethos`
- Not a fork of Debian. It takes Debian's `.deb` files, converts them, and lays
  them out Arch-style (merged `/usr`, no multiarch triplet, `sbin` into `bin`).

## Architecture

```
Debian trixie archive  --.deb-->  npkg convert  --.npk-->  Arch-style root
                                                              |
                        sway (wlroots) + WebKitGTK 6.0 + gtk4-layer-shell
                                                              |
                              panel / dock / menu / desktop  (HTML + CSS + JS)
                                                              |
                                     nethosd (Python HTTP API on 127.0.0.1:7777)
```

- **`pkg/npkg.py`** — the package manager. install/remove/list/info/files/owns/
  verify/search/index/fetch/convert/service/provides/check.
- **`pkg/npkg_convert.py`** — `.deb` and Arch `.pkg.tar.zst` conversion, plus
  the Arch relayout.
- **`pkg/npkg_rpm.py`** — `.rpm` reading, written from the format spec.
- **`pkg/npkg_elf.py`** — ELF `DT_SONAME`/`DT_NEEDED` parsing. This is what
  makes cross-distribution installs work: package *names* differ between
  distributions, sonames do not.
- **`pkg/npkg_bootstrap.py`** — builds a root filesystem from scratch. Package
  sets in `SETS`, suite in `SUITE` (currently `trixie`).
- **`pkg/npkg_service.py`** — enables systemd units by writing `.wants`
  symlinks, without systemctl.
- **`payload/`** — the desktop. `bin/nethos-view` hosts the WebKit surfaces,
  `nethosd/nethosd.py` is the API, `shell/*.html` + `shell/shell.js` are the UI.
- **`scripts/build-image.sh`** — builds a bootable arm64 image inside a Debian
  VM under HVF. **arm64 only.**
- **`scripts/run.sh`** — boots the image. `--arch aarch64|x86_64`.

## Build and run

```bash
bash scripts/build-image.sh          # ~4 minutes, needs the VM to be shut down
bash scripts/run.sh --arch aarch64
```

Login: `neth` / `nethos`. Root password also `nethos`. `sudo` works, `neth` is
in `wheel`.

**The build overwrites `build/nethos-arm.qcow2` — the same disk `run.sh` boots.
Shut the VM down first, and expect to lose anything done inside it.**

`build/nethos-pkgcache.qcow2` persists between builds and holds the downloaded
`.deb` files. Delete it to force a fresh download; it costs ~490 MB.

## The one thing to understand before changing anything

**npkg does not run Debian maintainer scripts.** Nearly every bug in this
project has been a `postinst` that never ran, and each one presents as a
completely different mystery symptom. Found so far, all now handled in
`scripts/build-image.sh` or `npkg_bootstrap.py`:

| Missing step | How it presented |
| --- | --- |
| `pam-auth-update` | `PAM Failure, aborting` — nobody can log in, including root |
| `update-alternatives` | `vim: command not found` after installing vim |
| `depmod` | `ALERT! UUID=... does not exist` — no root filesystem |
| `systemctl preset` | boots to a serial console, no desktop |
| `glib-compile-schemas` | GTK apps abort: "No GSettings schemas are installed" |
| `update-ca-certificates` | "System trust contains zero trusted certificates" |
| `fc-cache` | every GUI app rebuilds the font cache on first launch |
| `ssh-keygen -A` + `sshd` user | sshd refuses to start |
| `sshd_config` install | Debian ships the template in `/usr/share/openssh/` |

**The right fix is a trigger system in npkg** — run the known post-install
actions after a transaction, so installing a package rebuilds the caches it
invalidates. This is the most valuable outstanding work. Right now the actions
are hardcoded in the image build, which does nothing for packages installed
later with `npkg fetch`.

## Fixed this session (all committed and pushed)

- **Desktop would not start.** Four stacked silent failures: `getty@tty1` never
  enabled; `LIBGL_ALWAYS_SOFTWARE=1` forced, which Mesa rejects against a real
  DRM node so EGL never initialised and sway died in 69 ms; the
  gtk4-layer-shell preload tested for the bare `.so` (a `-dev` symlink that is
  not installed) so panels silently became plain windows; GSettings schemas
  uncompiled.
- **White screen.** `Gdk.RGBA(red=0, green=0, blue=0, alpha=0)` does nothing —
  PyGObject ignores constructor keywords on boxed types, warns, and continues.
  The struct was uninitialised, so WebKit painted every surface opaque. Assign
  the fields after construction.
- **Launcher appeared frozen, then did everything at once.** `set_visible(False)`
  stops GTK's frame clock and it does not restart on show. The menu had
  `visibilityState: visible` and **zero** `requestAnimationFrame` callbacks
  while every other surface ran at 59 fps. WebKit drives input dispatch off its
  rendering pipeline, so a page with no frames never drains its click queue —
  ten clicks on htop opened twenty copies half a minute later. Fix is
  `queue_draw()` + `present()` on show, in `payload/bin/nethos-view`.
- **`npkg fetch` used bookworm on a trixie system.** The suite is now recorded
  at `/etc/npkg/suite` and read from there.
- **Build speed.** Downloads run 16-way, conversion runs across every core, and
  the package cache survives between builds. 545 packages / 487 MB went from
  6.3 s to 0.03 s on a warm cache. Full rebuild is ~4 minutes.
- **Three build-system bugs that hid failures rather than causing them:**
  `set -o pipefail` plus `ls` on an empty glob aborting with no message; a
  failed build leaving its VM alive holding disk locks so the *next* build died
  instantly; and the host script printing `Built:` regardless of whether the
  guest succeeded. All three are fixed — the last one matters most, because a
  broken image could have been reported as good.

## Known open items

1. **Performance in the VM.** `virtio_gpu` has no virgl, so there is no
   hardware GL: sway's compositing and WebKit's rendering both go through
   llvmpipe on the CPU. Measured after the frame-clock fix: shell surfaces at
   50–59 fps, load 0.11 on 4 cores, app launch 0.28 s end to end. The user still
   reports sluggishness. **This has not been measured since the last rebuild —
   measure before theorising.** Use a `requestAnimationFrame` probe injected
   into `shell.js`; that is how the frame-clock bug was found, after three
   wrong guesses.
2. **No x86 build.** `build-image.sh` is arm64-only. `build/nethos.qcow2` is
   from the older Arch-based flow and has none of these fixes. Running on the
   user's real x86 PC requires porting the build, which is real work.
3. **No `openssh-server` in the package set.** Remote access has to be
   installed by hand every rebuild. Adding it would save a lot of relaying.
   Note the default password is `nethos`, so think before enabling it by
   default on a network.
4. **Hyprland.** Not packaged in Debian at all. sway is used because it is the
   only packaged wlroots compositor with an IPC socket, which the panel and dock
   require. The cost is no blur and no rounded window corners.

## Debugging technique that worked

The desktop writes nothing to a terminal, so:

- sway's own output goes to `~/.cache/sway.log` (set up in `.bash_profile`).
- `nethos-view` enables `set_enable_write_console_messages_to_stdout`, so
  **`console.log` from any shell page lands in that log**. Injecting a probe
  into `/usr/share/nethos/shell/shell.js` and running `nethos-reload` is the
  fastest way to see what the UI is actually doing.
- `swaymsg -t get_tree` / `-t get_seats` for compositor state. Layer surfaces do
  not appear in the tree; only regular windows do.
- The nethosd API answers in 1–8 ms and is rarely the problem:
  `curl -s http://127.0.0.1:7777/api/windows`.

Measure before concluding. In this session the frame-clock bug was reached only
after CPU starvation, nethosd latency, the launch path, pointer delivery, and
the shell's JavaScript had each been measured and ruled out — and after three
confident wrong diagnoses.
