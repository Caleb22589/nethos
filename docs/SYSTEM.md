# The NETHOS system

A bootable aarch64 system built from Debian packages converted into npkg's own
format, laid out and administered the Arch way. Verified booting on Apple
Silicon under HVF.

```
NETHOS Linux 6.1.0-50-arm64 aarch64
nethos login: neth
neth@nethos ~ $ sudo id
uid=0(root) gid=0(root) groups=0(root)
```

## Build and run

```bash
scripts/build-image.sh          # ~4 minutes on an M-series Mac
scripts/run.sh --reset-uefi     # boot it
```

`neth` / `nethos`, root password `nethos`. Change both.

Use `--reset-uefi` whenever the VM lands on a `Shell>` prompt: that is the UEFI
variable store having lost its boot entries, usually from a VM killed
mid-write.

## What is ours and what is Debian's

Ours: the package manager, package format, package database, filesystem
layout, user and group setup, PAM stacks, sudoers, bootstrap and image build.
There is **no dpkg and no apt on the image**.

Debian's: the compiled binaries, and the kernel build.

There is no third option short of compiling a userland from source, which for
anything reaching a browser is months of work. Ubuntu and Mint make the same
trade with less divergence, since they keep dpkg.

## Layout

```
/bin  /sbin  /lib  /lib64  ->  symlinks into /usr
/usr/bin      everything executable, Debian's sbin merged in
/usr/lib      every library, multiarch triplet flattened
/usr/lib/aarch64-linux-gnu -> .   compatibility for hardcoded paths
wheel         the administrators' group
```

The skeleton is created *before* any package is installed. Every ELF binary has
its interpreter path compiled in (`/lib/ld-linux-aarch64.so.1`), so `/lib` must
resolve into `/usr/lib` before anything can execute at all.

## What dropping dpkg actually costs

Worth stating plainly, because each of these cost a build cycle to find:

- **Some of Debian's `/etc` is generated, not packaged.** `pam-auth-update`
  builds `/etc/pam.d/common-*` in a postinst. Without them nobody can log in —
  not even root. The bootstrap writes them itself.
- **Maintainer scripts cannot be trusted to run.** They expect dpkg, a debconf
  database and their own arguments. debconf's postinst blocks on stdin
  forever. They are not carried over unless `--scripts` is passed, and hooks
  run with stdin closed and a timeout.
- **Essential packages hide their dependencies in `Pre-Depends`.** Reading only
  `Depends` makes the entire base system's requirements invisible.
- **Some required packages are depended on by nothing.** `libc-bin` owns `ldd`
  and `ldconfig`; it is in a base system because it is `Priority: required`.
  The base set is derived from Debian's own `Essential` and `Priority` fields
  rather than hand-listed.

## Known rough edges

- **`tar` is not installed.** It collides with `cpio`: both ship
  `/usr/sbin/rmt`, which becomes the same path once sbin merges into bin. A
  direct consequence of the Arch layout, and the clearest example of what that
  choice costs. Fixable by treating `rmt` as a diversion.
- **GRUB prints `terminal 'serial' isn't found`** — harmless; arm64 GRUB has no
  serial terminal module and the config asks for one.
- **The desktop is not on this system.** The NETHOS shell, `nethosd`, dock and
  widgets were built against the Arch payload and its package names. Porting
  them to this base is its own piece of work.
- **No network tooling by default.** `--sets "base system kernel net"` adds
  NetworkManager and wpa_supplicant.

## Rebuilding from a different base

Everything is parameterised: `--suite`, `--mirror`, `--arch`. Pointing at
Ubuntu's archive, or at `trixie` instead of `bookworm`, is a flag rather than a
rewrite. Keep to one upstream — package names differ between distributions and
the solver will ask for names the other one does not have.
