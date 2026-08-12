# NETHOS

A Linux distribution with its own package manager and a desktop written in
HTML and CSS.

It boots on real x86-64 and arm64 hardware, installs, persists, and manages
itself. Packages come from Debian's archive; almost nothing else does.

```bash
scripts/build-x86.sh      # a bootable image, ~3 minutes with KVM
scripts/make-usb.sh       # a flashable disk image
scripts/run.sh            # or try it in a VM first
```

Log in as `neth` / `nethos`.

---

## What is actually different here

Plenty of distributions are a familiar base with new defaults. These are the
parts of NETHOS that are not that.

### Dependencies resolve by soname, not by package name

Package names disagree between distributions and always will:

```
Debian  libssl3        Arch  openssl      Fedora  openssl-libs
```

They all install a file that calls itself `libssl.so.3`, and every binary
linked against it asks for exactly that string. So npkg indexes what is
*installed* rather than what somebody called it. At install time it reads the
ELF `DT_SONAME` of every library it lays down, and a requirement is satisfied
by, in order:

1. an installed package of that name at an acceptable version
2. a virtual name something declares in `Provides`
3. a **soname** carried by an installed library
4. a **file path** owned by an installed package

Three and four are why a Fedora package can install onto a Debian-derived root.
Verified end to end: `tree` from **Rocky Linux**, converted from `.rpm`,
requiring `libc.so.6` and `ld-linux-aarch64.so.1`, installed onto a root built
entirely from Debian packages — its requirements resolved against Debian's
`libc6`, and `npkg check` confirmed all 282 binaries across the installed set
could find every library they ask for.

```bash
npkg provides libc.so.6      # libc.so.6 is provided by libc6
npkg check                   # every binary can find its libraries
```

### One tool reads three package formats, from the format specs

`.deb`, Arch's `.pkg.tar.zst`, and `.rpm` are all read directly — an `ar`
archive, a tarball with a `.PKGINFO`, and a lead plus binary headers plus a
compressed cpio. No `dpkg`, no `rpm`, no `libalpm`, no `alien`. Pure Python
standard library, because the system has to be able to read its own packages
before any of those tools exist on it.

```bash
npkg convert firefox.deb
npkg convert firefox.pkg.tar.zst
npkg convert firefox.rpm
```

`docs/MULTIDISTRO.md` is honest about where this works and where it does not.

### The desktop is HTML and CSS, and they are real desktop surfaces

Not a browser in kiosk mode, and not Electron. The panel, dock, launcher and
desktop are genuine `wlr-layer-shell` surfaces hosted by WebKitGTK: the
compositor reserves space for the panel's exclusive zone, they never tile,
never take focus by accident, and never appear in the window list. They are
panels, drawn with CSS.

Window management is real too — the taskbar lists and controls actual windows
over sway's IPC.

### The whole desktop reloads live

```bash
nethos-reload
```

Every surface reloads instantly. No session restart, no logout. Edit
`/usr/share/nethos/shell/style.css`, run that, and the change is on screen.
The shell is the part of the system that is *meant* to be edited.

### It can tell you what it is doing

```bash
nethos-doctor
```

Prints whether the daemon is reachable, how many descriptors it holds, seconds
since each surface last reported in, recent JavaScript errors, the recent
request log, and which web process is burning CPU. A desktop that reports its
own health is rarer than it should be — see `docs/INTERNALS.md` for the
several days of guesswork that produced it.

### Packages install without running maintainer scripts

npkg never executes a package's `postinst`. Installing a package cannot run
arbitrary code as root.

That is a real safety property with a real cost, and the cost is documented
rather than hidden: ten separate things Debian does from maintainer scripts had
to be reimplemented in the build, and each one first appeared as a completely
unrelated-looking bug. `docs/INTERNALS.md` lists all ten.

---

## What it is built from, honestly

- **Debian's binary packages and kernel.** NETHOS is Debian-derived, the way
  Ubuntu and Mint are. Building from source is a different project.
- **sway** as the compositor. Hyprland is not packaged in Debian at all, and of
  the packaged wlroots compositors only sway exposes the IPC socket the panel
  and dock need. Blur and rounded window corners are the price.
- **WebKitGTK** for the shell, **Chromium** as the browser.

## Layout

```
pkg/npkg.py            the package manager
pkg/npkg_convert.py    .deb and Arch conversion, and the Arch relayout
pkg/npkg_rpm.py        .rpm reading
pkg/npkg_elf.py        DT_SONAME / DT_NEEDED - the capability index
pkg/npkg_bootstrap.py  building a root filesystem from nothing
pkg/npkg_service.py    enabling systemd units without systemctl
payload/               the desktop: shell, nethosd, nethos-view, apps
scripts/               build, run, and flash
docs/                  everything below
```

The filesystem is Arch-shaped: merged `/usr`, Debian's multiarch triplet
flattened away, `sbin` merged into `bin`, `wheel` for administrators.

## Documentation

| | |
| --- | --- |
| `docs/INTERNALS.md` | how it works, and the bugs that shaped it |
| `docs/PACKAGES.md` | npkg in use |
| `docs/MULTIDISTRO.md` | cross-distribution packages, including the limits |
| `docs/APPS.md` | writing apps for the shell |
| `docs/SYSTEM.md` | the system layout |
| `docs/UPDATING.md` | updating from the repository |
| `docs/HANDOFF.md` | current state and open problems |

## Status

Boots and runs on real hardware. Not finished. `docs/HANDOFF.md` keeps the
current list of what is known to be broken.
