#!/usr/bin/env python3
"""
npkg_bootstrap — build a root filesystem from converted Debian packages.

Debian supplies the packages; the result is laid out and administered the Arch
way. That means:

    /usr/bin        everything executable, sbin merged in
    /usr/lib        every library, Debian's multiarch triplet flattened away
    /bin /sbin /lib /lib64 /usr/sbin   compatibility symlinks into /usr
    wheel           the administrators' group, as on Arch
    /home/<user>    a real home, from /etc/skel

    npkg-bootstrap /mnt/newroot --user caleb
    npkg-bootstrap /mnt/newroot --set desktop --arch arm64

Solving happens against Debian's own Packages index, because that is where
the dependency truth lives; only then are the .debs converted and installed.

Run as root for a bootable result: setuid bits on sudo and su survive the
tarball, but file ownership can only be applied by root, and a sudo owned by
you rather than root will refuse to run.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import gzip
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from npkg import Database, Manifest, NpkgError, Package, Transaction, build_index  # noqa: E402
from npkg_convert import convert_deb, parse_control  # noqa: E402

def say(*args, **kwargs):
    """print(), but visible immediately even through a pipe."""
    kwargs.setdefault('flush', True)
    print(*args, **kwargs)


MIRROR = "http://deb.debian.org/debian"
SUITE = "trixie"

# A base that boots to a shell and can administer itself. Debian pulls the rest
# in through dependencies; these are only the things worth naming.
SETS = {
    "base": [
        "base-files", "libc6", "coreutils", "bash", "dash",
        "util-linux",            # su, mount, login
        "passwd",                # useradd, passwd, chpasswd
        "sudo",
        "findutils", "grep", "sed", "gawk", "diffutils", "tar", "gzip",
        "ncurses-base", "ncurses-bin", "libtinfo6",
        "debianutils", "hostname", "init-system-helpers",
    ],
    "system": [
        # systemd is pid 1. systemd-sysv is what provides /sbin/init, which is
        # what the kernel actually looks for.
        "systemd", "systemd-sysv", "udev", "dbus", "kmod", "libcap2-bin",
        "iproute2", "iputils-ping", "netbase", "ca-certificates",
        "nano", "less", "procps", "psmisc", "e2fsprogs", "mount",
        # pciutils and usbutils: lspci and lsusb are the first commands anyone
        # reaches for when hardware does not work, and their absence turns a
        # thirty second answer into an evening. They are under 2MB.
        "pciutils", "usbutils", "rfkill",
        # npkg is Python, so the system needs one to manage itself. curl and
        # ca-certificates are what it fetches packages with.
        "python3", "python3-minimal", "curl", "wget", "ca-certificates",
        # git: nethos-update pulls the repository in place. Without it the
        # only way to change a line of CSS is a full image rebuild.
        "git",
        # openssh-server, because a desktop that misbehaves is far easier to
        # diagnose over a shell than over photographs of a screen. Installed,
        # not enabled -- see the build, which sets it up but leaves the service
        # off. `systemctl enable --now ssh` turns it on deliberately.
        "openssh-server", "openssh-client",
        # growpart, for filling the disk the image was written to. The image is
        # small so that flashing is quick; without this a 6G image on a 256G
        # SSD leaves 250G unreachable and tells the user to resize it by hand.
        "cloud-guest-utils",
        # update-ca-certificates is a shell script that shells out to openssl.
        # Without the binary it exits non-zero and writes no bundle, so
        # /etc/ssl/certs stays empty and every TLS client reports zero trusted
        # certificates -- with the certificates themselves present the whole
        # time. ca-certificates depends on openssl only as Recommends, and we
        # do not install recommends.
        "openssl",
    ],
    "kernel": [
        # {arch} is substituted for the Debian architecture being built.
        "linux-image-{arch}",
        # Debian's arm64 kernel has virtio as modules, so the root device is
        # unreachable without an initramfs to load them first.
        "initramfs-tools", "busybox", "zstd",
        "grub-efi-{arch}-bin", "grub-common", "grub2-common", "efibootmgr",
        # Firmware is its own set: see "firmware" below. It is several hundred
        # megabytes and a VM needs none of it.
    ],
    "net": ["network-manager", "wpasupplicant", "iw", "wireless-regdb"],

    # Real hardware only, and the single largest optional thing in the system.
    # Modesetting on AMD and Intel graphics, and most wifi, load firmware blobs
    # at probe time; without them a machine that works perfectly under QEMU
    # gives a black screen and no network. Several hundred megabytes, so a VM
    # image should not carry it: build with --sets "base system kernel desktop"
    # for a VM and add "firmware" for hardware.
    "firmware": [
        "firmware-linux-free", "firmware-misc-nonfree",
        "firmware-amd-graphics",
        # Wi-Fi, and all of it rather than a guess. Only Intel and Realtek were
        # here, so a Broadcom, Atheros or MediaTek card loaded its driver, found
        # no firmware and reported no networks -- which looks exactly like a
        # broken wifi stack rather than a missing file. These are a few tens of
        # megabytes between them and they remove a whole class of "it does not
        # work on my machine".
        "firmware-iwlwifi",          # Intel
        "firmware-realtek",          # Realtek
        "firmware-brcm80211",        # Broadcom -- very common in laptops
        "firmware-atheros",          # Qualcomm Atheros
        "firmware-mediatek",         # MT76xx, common in recent AMD machines
        "firmware-ti-connectivity",
        "firmware-libertas",
        # regulatory.db. Without it cfg80211 falls back to a domain with
        # effectively no permitted channels, so a card whose driver and
        # firmware both loaded correctly scans and finds nothing -- which
        # looks exactly like broken wifi. It lived only in the "net" set,
        # which the default build does not include, while network-manager and
        # wpasupplicant were duplicated into "desktop" and made networking
        # look complete. It is about 10KB.
        "wireless-regdb",
        # iw, so the regulatory domain can be inspected and set by hand.
        "iw",
    ],

    # The floor for an installer: enough to partition a disk, make filesystems,
    # reach the network, and run npkg. No systemd, no init system at all -- the
    # installer is meant to run from an initramfs as PID 1 and then reboot.
    #
    # python3-minimal rather than python3: npkg needs the standard library and
    # nothing else, and the difference is about 30MB. See docs/INSTALLER.md.
    "installer": [
        "busybox", "python3-minimal", "libpython3-stdlib",
        "parted", "e2fsprogs", "dosfstools", "util-linux",
        "curl", "ca-certificates", "openssl",
        "kmod", "pciutils",
    ],

    # A full browser. ~400MB, and separate because the shell does not need it:
    # WebKit draws everything NETHOS itself shows. Add it when you want one,
    # or `npkg fetch chromium` on a running system.
    "browser": ["chromium"],

    # The NETHOS desktop. Debian names, which differ from Arch's throughout.
    "desktop": [
        # compositor. Hyprland is not packaged by Debian at all, and of the
        # ones that are, only sway exposes an IPC socket -- which is what the
        # panel and dock use to list and control windows. Blur and rounded
        # window corners are the cost of that choice.
        # Hyprland first: it is the only packaged compositor that rounds a
        # corner and blurs behind a surface, and the blur matters for more than
        # looks. Done in the compositor it is nearly free; done in CSS with
        # backdrop-filter it is per-frame CPU work across the whole surface,
        # which is what made the shell unusable on software rendering. Real
        # glass, and cheaper than the imitation.
        #
        # sway stays installed as the fallback -- the session picks whichever is
        # present, and nethosd already speaks both IPCs.
        # Wayfire: floating windows, real decorations with minimise and
        # maximise, edge snapping on drag, and blur done in the compositor --
        # which is what makes the glass real instead of a per-frame CSS cost.
        # sway can do none of those four.
        # NOT wf-shell: it ships wf-panel and wf-background, and NETHOS has
        # its own panel, dock and wallpaper. Installing it puts a second bar
        # across the top of the screen and a second thing drawing the desktop.
        "wayfire", "wayfire-plugins-extra",
        # sway stays as the fallback so a machine where Wayfire will not start
        # still reaches a desktop.
        "sway", "swaybg", "swayidle", "swaylock", "xwayland",
        "seatd", "libseat1",

        # the shell: WebKit + GTK4 layer-shell, driven from Python
        # libglib2.0-bin carries glib-compile-schemas. GTK apps abort outright
        # with "No GSettings schemas are installed on the system" until the
        # .gschema.xml files on disk have been compiled, and nothing else in
        # the set can do it -- neither at build time nor after npkg installs
        # some later application that ships schemas of its own.
        # libgdk-pixbuf2.0-bin carries gdk-pixbuf-query-loaders, the last of
        # the cache builders we need; fontconfig carries fc-cache.
        # python3-cairo is not optional: nethos-view builds the dock's input
        # region with it, and without it the import raises, the failure is
        # swallowed, and the dock swallows every click in the strip along the
        # bottom of the screen -- so a browser cannot be scrolled there.
        "python3-gi", "python3-gi-cairo", "python3-cairo",
        "python3-dbus", "libgtk-4-1", "libglib2.0-bin",
        "libgdk-pixbuf2.0-bin", "fontconfig", "shared-mime-info",
        "desktop-file-utils",
        "gir1.2-gtk4layershell-1.0", "libgtk4-layer-shell0",
        "gir1.2-webkit-6.0", "libwebkitgtk-6.0-4",

        # Mesa, named rather than left to arrive as somebody's dependency.
        # These carry the guest half of virgl: with QEMU's virtio-vga-gl and
        # virglrenderer on the host, libgl1-mesa-dri provides the virtio_gpu
        # driver that turns the guest's GL into the host GPU's. Without it
        # everything falls back to llvmpipe on the CPU and the acceleration is
        # present on the host but unused, which is indistinguishable from not
        # having set it up at all.
        "libgl1-mesa-dri", "libegl-mesa0", "libglx-mesa0", "mesa-utils",

        # The everyday applications. Chromium is NOT here: it is ~400MB, which
        # is most of the difference between a lean image and a fat one, and the
        # shell already has WebKit for anything NETHOS draws itself. It lives
        # in the "browser" set, or one command away:  npkg fetch chromium
        "foot", "thunar", "mousepad", "imv", "htop",
        "wl-clipboard", "brightnessctl", "xdg-utils",
        "xdg-desktop-portal", "xdg-desktop-portal-wlr",

        # fonts and icons the shell asks for by name
        "fonts-inter", "fonts-dejavu", "fonts-jetbrains-mono",
        # adwaita only: GTK needs it. Papirus is another ~80MB, and the shell
        # falls back to initials for anything it cannot find.
        "fonts-noto-color-emoji", "hicolor-icon-theme", "adwaita-icon-theme",

        # networking, so the desktop can get online on real hardware
        "network-manager", "wpasupplicant",
    ],
}

SKEL_PROFILE = """\
# ~/.bash_profile
[[ -f ~/.bashrc ]] && . ~/.bashrc
"""

SKEL_BASHRC = """\
# ~/.bashrc
[[ $- != *i* ]] && return

PS1='\\[\\e[38;5;39m\\]\\u\\[\\e[0m\\]@\\h \\[\\e[38;5;245m\\]\\w\\[\\e[0m\\] $ '
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias ll='ls -lah'
export EDITOR=nano
"""


# ---------------------------------------------------------------------------
# Debian archive
# ---------------------------------------------------------------------------

class DebianArchive:
    """Just enough of a Debian mirror to resolve and fetch a package set."""

    def __init__(self, mirror=MIRROR, suite=SUITE, arch="arm64",
                 components=("main", "non-free-firmware"), cache="."):
        self.mirror = mirror.rstrip("/")
        self.suite = suite
        self.arch = arch
        self.components = components
        self.cache = cache
        self.packages: dict[str, dict] = {}

    def load(self) -> None:
        for component in self.components:
            url = (f"{self.mirror}/dists/{self.suite}/{component}/"
                   f"binary-{self.arch}/Packages.gz")
            cached = os.path.join(self.cache, f"Packages-{component}-{self.arch}.gz")
            if not os.path.isfile(cached):
                say(f"  fetching {component}/{self.arch} index")
                os.makedirs(self.cache, exist_ok=True)
                try:
                    with urllib.request.urlopen(url, timeout=60) as resp, \
                            open(cached, "wb") as fh:
                        shutil.copyfileobj(resp, fh)
                except (urllib.error.URLError, OSError) as exc:
                    raise NpkgError(f"cannot fetch {url}: {exc}") from exc

            with gzip.open(cached, "rt", encoding="utf-8", errors="replace") as fh:
                for stanza in fh.read().split("\n\n"):
                    if not stanza.strip():
                        continue
                    fields = parse_control(stanza)
                    name = fields.get("Package")
                    if name and name not in self.packages:
                        self.packages[name] = fields
        say(f"  index: {len(self.packages)} packages available")

    def base_seeds(self) -> list[str]:
        """Every Essential and Priority: required package.

        This is what debootstrap means by a base system, and it is the right
        answer to "why is libc-bin missing": nothing declares a dependency on
        it, yet ldd and ldconfig live there and initramfs-tools cannot work
        without them. Hand-listing the base was always going to keep springing
        leaks; Debian already marks what belongs.
        """
        out = []
        for name, fields in self.packages.items():
            if (fields.get("Essential", "").lower() == "yes"
                    or fields.get("Priority", "") == "required"):
                out.append(name)
        return out

    def provider(self, name: str) -> dict | None:
        """Find a package by name, or by what it provides (a virtual name)."""
        if name in self.packages:
            return self.packages[name]
        for fields in self.packages.values():
            provides = fields.get("Provides", "")
            if provides and name in [p.split()[0].strip()
                                     for p in provides.split(",") if p.strip()]:
                return fields
        return None

    def resolve(self, seeds: list[str], with_recommends: bool = False) -> list[dict]:
        """Walk Depends breadth-first. Debian's own metadata is the authority."""
        chosen: dict[str, dict] = {}
        queue = list(seeds)
        missing: set[str] = set()

        while queue:
            want = queue.pop(0)
            base = want.split()[0].split(":")[0].strip()
            if base in chosen or base in missing:
                continue
            fields = self.provider(base)
            if fields is None:
                missing.add(base)
                continue
            chosen[fields["Package"]] = fields

            # Pre-Depends, not just Depends. Debian's essential packages --
            # coreutils, libc6, dpkg -- declare their requirements there, so
            # reading only Depends makes every dependency of the base system
            # invisible. That is how libattr1 and libc-bin went missing while
            # the resolver reported nothing wrong.
            deps = ", ".join(filter(None, [fields.get("Pre-Depends", ""),
                                           fields.get("Depends", "")]))
            if with_recommends:
                deps += ", " + fields.get("Recommends", "")
            for clause in deps.split(","):
                clause = clause.strip()
                if not clause:
                    continue
                # Alternatives: take the first, the conventional preference.
                first = clause.split("|")[0].strip().split()[0]
                queue.append(first.split(":")[0])

        if missing:
            say(f"  note: {len(missing)} names had no package "
                  f"(virtual or unavailable): {', '.join(sorted(missing)[:6])}")
        return list(chosen.values())

    def download(self, fields: dict, outdir: str) -> str:
        filename = fields["Filename"]
        target = os.path.join(outdir, os.path.basename(filename))

        # A cached file is only usable if it is the whole file. An interrupted
        # download leaves a short .deb behind, and "does it exist" then keeps
        # it forever: conversion fails with "Compressed file ended before the
        # end-of-stream marker was reached", the package is skipped with a
        # warning nobody reads, and it is missing from every image built from
        # that cache afterwards. The archive tells us the size, so check it.
        want = int(fields.get("Size", 0) or 0)
        if os.path.isfile(target):
            if not want or os.path.getsize(target) == want:
                return target
            say(f"  re-fetching {os.path.basename(filename)} "
                f"({os.path.getsize(target)} bytes, expected {want})")
            os.remove(target)
        os.makedirs(outdir, exist_ok=True)
        url = f"{self.mirror}/{filename}"
        tmp = target + ".part"
        
        last_exc = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
                # A connection closed early gives a short file and no
                # exception at all, so retrying on errors alone is not
                # enough -- that is how a truncated .deb reaches the cache
                # and stays there. Check the length the archive promised.
                got = os.path.getsize(tmp)
                if want and got != want:
                    raise OSError(f"truncated: got {got} bytes, expected {want}")
                os.replace(tmp, target)
                return target
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                import time
                time.sleep(1 * (attempt + 1))
        
        raise NpkgError(f"download failed after 3 attempts: {url}: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# the Arch-shaped root
# ---------------------------------------------------------------------------

def make_skeleton(root: str) -> None:
    """Create the directory tree and the compatibility symlinks.

    These must exist before anything is installed: every ELF binary hardcodes
    its interpreter path (/lib/ld-linux-aarch64.so.1), so /lib has to resolve
    into /usr/lib or nothing runs at all.
    """
    for d in ("usr/bin", "usr/lib", "usr/share", "usr/include", "usr/local",
              "etc", "var/log", "var/lib", "var/cache", "var/tmp",
              "home", "root", "proc", "sys", "dev", "run", "tmp", "boot", "mnt", "opt"):
        os.makedirs(os.path.join(root, d), exist_ok=True)

    os.chmod(os.path.join(root, "tmp"), 0o1777)
    os.chmod(os.path.join(root, "var/tmp"), 0o1777)
    os.chmod(os.path.join(root, "root"), 0o700)

    for link, target in (("bin", "usr/bin"), ("sbin", "usr/bin"),
                         ("lib", "usr/lib"), ("lib64", "usr/lib"),
                         ("usr/sbin", "bin"), ("usr/lib64", "lib")):
        path = os.path.join(root, link)
        if not os.path.islink(path) and not os.path.exists(path):
            os.symlink(target, path)

    # Anything with a multiarch path compiled into it still resolves.
    for triplet in ("aarch64-linux-gnu", "x86_64-linux-gnu"):
        path = os.path.join(root, "usr/lib", triplet)
        if not os.path.exists(path) and not os.path.islink(path):
            os.symlink(".", path)


def hash_password(password: str) -> str:
    """SHA-512 crypt via openssl.

    Python's crypt module was removed in 3.13 and there is no stdlib
    replacement, so this shells out rather than vendoring an implementation.
    """
    if not shutil.which("openssl"):
        return "*"                       # locked account rather than a bad hash
    result = subprocess.run(["openssl", "passwd", "-6", password],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "*"


def setup_users(root: str, username: str, password: str,
                root_password: str, shell: str = "/bin/bash") -> None:
    """Users, groups, homes and the wheel rule — the Arch arrangement."""
    uid, gid = 1000, 1000
    etc = os.path.join(root, "etc")
    os.makedirs(etc, exist_ok=True)

    groups = [
        ("root", 0, []), ("wheel", 10, [username]), ("users", 100, [username]),
        ("tty", 5, []), ("disk", 6, []), ("audio", 29, [username]),
        ("video", 44, [username]), ("input", 97, [username]),
        ("kvm", 78, []), ("render", 105, []), ("nogroup", 65534, []),
        (username, gid, []),
    ]
    with open(os.path.join(etc, "group"), "w") as fh:
        for name, number, members in groups:
            fh.write(f"{name}:x:{number}:{','.join(members)}\n")

    with open(os.path.join(etc, "passwd"), "w") as fh:
        fh.write("root:x:0:0:root:/root:/bin/bash\n")
        fh.write("nobody:x:65534:65534:Nobody:/nonexistent:/usr/bin/nologin\n")
        fh.write(f"{username}:x:{uid}:{gid}:{username}:/home/{username}:{shell}\n")

    shadow = os.path.join(etc, "shadow")
    with open(shadow, "w") as fh:
        fh.write(f"root:{hash_password(root_password)}:19000:0:99999:7:::\n")
        fh.write("nobody:*:19000:0:99999:7:::\n")
        fh.write(f"{username}:{hash_password(password)}:19000:0:99999:7:::\n")
    os.chmod(shadow, 0o640)

    with open(os.path.join(etc, "gshadow"), "w") as fh:
        for name, _number, members in groups:
            fh.write(f"{name}:!::{','.join(members)}\n")
    os.chmod(os.path.join(etc, "gshadow"), 0o640)

    # /etc/skel, then the user's home from it.
    skel = os.path.join(etc, "skel")
    os.makedirs(skel, exist_ok=True)
    with open(os.path.join(skel, ".bash_profile"), "w") as fh:
        fh.write(SKEL_PROFILE)
    with open(os.path.join(skel, ".bashrc"), "w") as fh:
        fh.write(SKEL_BASHRC)

    home = os.path.join(root, "home", username)
    os.makedirs(home, exist_ok=True)
    for entry in os.listdir(skel):
        target = os.path.join(home, entry)
        if not os.path.exists(target):
            shutil.copy2(os.path.join(skel, entry), target)
    for sub in (".config", ".local/share", ".local/state", ".cache"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)

    if os.geteuid() == 0:
        for base, dirs, files in os.walk(home):
            os.chown(base, uid, gid)
            for name in dirs + files:
                os.chown(os.path.join(base, name), uid, gid)
        os.chown(home, uid, gid)
    os.chmod(home, 0o750)


def setup_network(root: str) -> None:
    """Bring the wired interface up with DHCP.

    Without this the system boots with an interface and no address: the kernel
    renames eth0 to enp0s4 and then nothing configures it, so there is no
    default route and no DNS. `npkg fetch` fails at the first download and it
    looks like a package manager bug rather than a missing network.

    systemd-networkd rather than NetworkManager, because it is already present
    (systemd is pid 1) and needs one config file instead of a daemon and a
    dependency chain.
    """
    netdir = os.path.join(root, "etc/systemd/network")
    os.makedirs(netdir, exist_ok=True)
    with open(os.path.join(netdir, "20-wired.network"), "w") as fh:
        fh.write("[Match]\n"
                 "Name=en* eth*\n\n"
                 "[Network]\n"
                 "DHCP=yes\n"
                 "IPv6AcceptRA=yes\n\n"
                 "[DHCPv4]\n"
                 "UseDNS=yes\n"
                 "UseNTP=yes\n")

    # `systemctl enable` is only a symlink into a .wants directory, and we
    # cannot run systemctl against a root that is not booted -- so make the
    # symlinks directly.
    def enable(unit: str, target: str) -> None:
        wants = os.path.join(root, "etc/systemd/system", target + ".wants")
        os.makedirs(wants, exist_ok=True)
        link = os.path.join(wants, unit)
        if not os.path.islink(link) and not os.path.exists(link):
            os.symlink("/usr/lib/systemd/system/" + unit, link)

    enable("systemd-networkd.service", "multi-user.target")
    enable("systemd-networkd.socket", "sockets.target")
    enable("systemd-resolved.service", "multi-user.target")


def install_npkg(root: str) -> None:
    """Put npkg on the system it just built.

    Without this the image has a package database and no way to read it: you
    could build a system but never install anything into it afterwards.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    dest = os.path.join(root, "usr/lib/nethos/pkg")
    os.makedirs(dest, exist_ok=True)
    for name in os.listdir(here):
        if name.endswith(".py"):
            shutil.copy2(os.path.join(here, name), os.path.join(dest, name))

    wrapper = os.path.join(root, "usr/bin/npkg")
    with open(wrapper, "w") as fh:
        fh.write("#!/bin/sh\n"
                 "# npkg lives in /usr/lib/nethos/pkg; this keeps its modules\n"
                 "# together and off the general Python path.\n"
                 'exec python3 /usr/lib/nethos/pkg/npkg.py "$@"\n')
    os.chmod(wrapper, 0o755)

    # Somewhere for repositories to be configured later.
    conf = os.path.join(root, "etc/npkg")
    os.makedirs(conf, exist_ok=True)
    repos = os.path.join(conf, "repos.json")
    if not os.path.exists(repos):
        with open(repos, "w") as fh:
            fh.write('{\n  "repos": []\n}\n')


def install_desktop(root: str, payload: str, username: str) -> None:
    """Install the NETHOS shell onto the built root.

    The shell itself is distribution-agnostic -- HTML, CSS, a Python daemon and
    a WebKit host -- so porting it is a file copy plus the session wiring that
    install-nethos.sh used to do with pacman. Only the package names differed,
    and those live in the desktop set.
    """
    prefix = os.path.join(root, "usr/share/nethos")
    for part in ("shell", "lib", "apps"):
        src = os.path.join(payload, part)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(prefix, part)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)

    bindir = os.path.join(root, "usr/bin")
    os.makedirs(bindir, exist_ok=True)
    daemon = os.path.join(payload, "nethosd", "nethosd.py")
    if os.path.isfile(daemon):
        shutil.copy2(daemon, os.path.join(bindir, "nethosd"))
        os.chmod(os.path.join(bindir, "nethosd"), 0o755)
    for name in sorted(os.listdir(os.path.join(payload, "bin"))):
        src = os.path.join(payload, "bin", name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(bindir, name))
            os.chmod(os.path.join(bindir, name), 0o755)

    # compositor config
    swaydir = os.path.join(root, "etc/sway")
    os.makedirs(os.path.join(swaydir, "config.d"), exist_ok=True)
    sway_src = os.path.join(payload, "sway", "config")
    if os.path.isfile(sway_src):
        shutil.copy2(sway_src, os.path.join(swaydir, "config"))

    hypr_src = os.path.join(payload, "hypr", "hyprland.conf")
    if os.path.isfile(hypr_src):
        os.makedirs(os.path.join(root, "etc/nethos"), exist_ok=True)
        shutil.copy2(hypr_src, os.path.join(root, "etc/nethos/hyprland.conf"))

    # nethosd as a user service, so restarting it is a second rather than a
    # logout. Enabled per-user at login by the session script.
    unit_src = os.path.join(payload, "systemd", "nethosd.service")
    if os.path.isfile(unit_src):
        userdir = os.path.join(root, "etc/systemd/user")
        os.makedirs(userdir, exist_ok=True)
        shutil.copy2(unit_src, os.path.join(userdir, "nethosd.service"))

    # System units from the payload. nethosd is per-user; growing the root
    # filesystem is emphatically not -- it has to happen once, early, before
    # anyone logs in.
    sysdir = os.path.join(root, "etc/systemd/system")
    os.makedirs(sysdir, exist_ok=True)
    for unit in ("nethos-growroot.service",):
        src = os.path.join(payload, "systemd", unit)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(sysdir, unit))

    # the user's session
    home = os.path.join(root, "home", username)

    # Wayfire reads ~/.config/wayfire.ini.
    _wf_src = os.path.join(payload, "wayfire", "wayfire.ini")
    if os.path.isfile(_wf_src):
        os.makedirs(os.path.join(home, ".config"), exist_ok=True)
        shutil.copy2(_wf_src, os.path.join(home, ".config", "wayfire.ini"))

    # Hyprland reads ~/.config/hypr/hyprland.conf, and nothing copies it there
    # at first login. Placed after `home` exists -- putting it earlier raised
    # UnboundLocalError and took the whole build with it.
    _hypr_user = os.path.join(home, ".config", "hypr")
    _hypr_src = os.path.join(payload, "hypr", "hyprland.conf")
    if os.path.isfile(_hypr_src):
        os.makedirs(_hypr_user, exist_ok=True)
        shutil.copy2(_hypr_src, os.path.join(_hypr_user, "hyprland.conf"))
    cfg = os.path.join(home, ".config", "sway")
    os.makedirs(cfg, exist_ok=True)
    link = os.path.join(cfg, "config")
    if not os.path.islink(link) and not os.path.exists(link):
        os.symlink("/etc/sway/config", link)

    with open(os.path.join(home, ".bash_profile"), "w") as fh:
        fh.write(
            "# ~/.bash_profile\n"
            "[ -f ~/.bashrc ] && . ~/.bashrc\n\n"
            "# Start the desktop on the first virtual terminal, nowhere else.\n"
            'if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then\n'
            "    export XDG_SESSION_TYPE=wayland XDG_CURRENT_DESKTOP=sway\n"
            "    export MOZ_ENABLE_WAYLAND=1 QT_QPA_PLATFORM=wayland\n"
            "    export GDK_BACKEND=wayland _JAVA_AWT_WM_NONREPARENTING=1\n"
            "    # wlroots refuses a software renderer unless told it may use\n"
            "    # one, and WebKit's compositing buffers are dear without a GPU.\n"
            "    export WLR_RENDERER_ALLOW_SOFTWARE=1\n"
            "    # virtio_gpu's atomic KMS does not reliably deliver page-flip\n"
            "    # completions, so wlroots waits for a flip that never lands\n"
            "    # and stops scheduling repaints. The desktop paints one\n"
            "    # partial frame and freezes until an input event kicks it --\n"
            "    # which reads as a white screen, dead buttons and a half\n"
            "    # drawn dock. The legacy path does not have the problem.\n"
            "    [ \"${NETHOS_NO_ATOMIC:-0}\" = 1 ] && "
            "export WLR_DRM_NO_ATOMIC=1\n"
            "    # WEBKIT_DISABLE_COMPOSITING_MODE forces WebKit to composite\n"
            "    # every page on the CPU. That was right when the VM had no\n"
            "    # GPU at all, and is exactly wrong once it has one: with virgl\n"
            "    # the guest can hand compositing to the host GPU, and keeping\n"
            "    # this set throws that away and leaves the shell as slow as it\n"
            "    # ever was. Only set it when there is genuinely no render node.\n"
            "    # GPU compositing is off unless asked for. Verified on real\n"
            "    # hardware: chromium drew a blank window and logged \"Frame\n"
            "    # latency is negative\"; chromium --disable-gpu rendered fine.\n"
            "    # WebKit fails identically, and that is the frozen clock and\n"
            "    # the dead buttons.\n"
            "    if [ \"${NETHOS_GPU_COMPOSITING:-0}\" = 1 ]; then\n"
            "        NETHOS_GPU=1; export NETHOS_GPU\n"
            "    else\n"
            "        export WEBKIT_DISABLE_COMPOSITING_MODE=1\n"
            "    fi\n"
            "    # No at-spi on NETHOS, so GTK's attempt to reach the\n"
            "    # accessibility bus only produces warnings on every launch.\n"
            "    export GTK_A11Y=none\n"
            "    # LIBGL_ALWAYS_SOFTWARE is deliberately NOT set. Mesa refuses\n"
            "    # it once wlroots opens a real DRM node -- 'Not allowed to\n"
            "    # force software rendering when API explicitly selects a\n"
            "    # hardware device' -- so EGL never initialises and sway exits\n"
            "    # in under a tenth of a second. The screen stays black, the\n"
            "    # session closes, getty restarts it, and systemd eventually\n"
            "    # gives up with start-limit-hit. virtio_gpu is a real DRM\n"
            "    # device even under emulation; let Mesa pick llvmpipe itself.\n"
            "    mkdir -p ~/.cache\n"
            "    # Logged, because a compositor that dies on tty1 writes its\n"
            "    # reason to a screen that is cleared before anyone reads it.\n"
            "    # Hyprland when it is installed, sway when it is not. Both\n"
            "    # give layer-shell surfaces and nethosd speaks both IPCs, so\n"
            "    # the shell is identical either way -- Hyprland just blurs\n"
            "    # behind it and rounds the corners.\n"
            "    if command -v wayfire >/dev/null 2>&1; then\n"
            "        wayfire >~/.cache/wayfire.log 2>&1 && exit\n"
            "        echo '--- wayfire failed; falling back ---' "
            ">>~/.cache/wayfire.log\n"
            "    fi\n"
            "    if [ \"${NETHOS_HYPRLAND:-0}\" = 1 ] && "
            "command -v Hyprland >/dev/null 2>&1; then\n"
            "        Hyprland >~/.cache/hyprland.log 2>&1 && exit\n"
            "        echo '--- Hyprland failed; falling back to sway ---' "
            ">>~/.cache/hyprland.log\n"
            "    fi\n"
            "    sway >~/.cache/sway.log 2>&1 && exit\n"
            "    # Second chance on the pixman renderer, which is CPU-only and\n"
            "    # touches no EGL at all. Slower, but a desktop.\n"
            "    echo '--- retrying with WLR_RENDERER=pixman ---' "
            ">>~/.cache/sway.log\n"
            "    WLR_RENDERER=pixman sway >>~/.cache/sway.log 2>&1 && exit\n"
            "    echo 'The desktop failed to start. Reason:'\n"
            "    tail -20 ~/.cache/sway.log\n"
            "fi\n")

    if os.geteuid() == 0:
        uid = gid = 1000
        for base, dirs, files in os.walk(home):
            for name in dirs + files:
                try:
                    os.lchown(os.path.join(base, name), uid, gid)
                except OSError:
                    pass
        os.chown(home, uid, gid)

    # autologin on tty1: the desktop should come up without a password prompt
    # in front of it, the way a laptop does.
    dropin = os.path.join(root, "etc/systemd/system/getty@tty1.service.d")
    os.makedirs(dropin, exist_ok=True)
    with open(os.path.join(dropin, "autologin.conf"), "w") as fh:
        fh.write("[Service]\nExecStart=\n"
                 f"ExecStart=-/usr/bin/agetty --autologin {username} "
                 "--noclear %I $TERM\n")

    # ...and getty@tty1 itself, which nothing else turns on. systemd's own
    # getty-generator only covers the console named by console= on the kernel
    # command line -- here that is ttyAMA0, so the serial getty starts and the
    # graphical one does not. The tty1 instance normally comes from `systemctl
    # preset` in Debian's postinst, which we do not run, so the drop-in above
    # would patch a service that never starts and .bash_profile would never run.
    #
    # Written as a symlink rather than `systemctl enable`: getty@ is a template
    # unit, and enabling it means exactly this symlink.
    wants = os.path.join(root, "etc/systemd/system/getty.target.wants")
    os.makedirs(wants, exist_ok=True)
    link = os.path.join(wants, "getty@tty1.service")
    if os.path.lexists(link):
        os.remove(link)
    os.symlink("/usr/lib/systemd/system/getty@.service", link)


def setup_pam(root: str) -> None:
    """Write the PAM stacks Debian would have generated.

    /etc/pam.d/common-auth and its siblings are not shipped by any package --
    libpam-runtime builds them in its postinst by running pam-auth-update over
    /usr/share/pam-configs. We do not run maintainer scripts, so nothing
    creates them, /etc/pam.d/login's "@include common-auth" fails, and login
    dies with "PAM Failure, aborting: Critical error - immediate abort".
    Nobody can log in, including root.

    These are the stacks a default Debian install ends up with, written
    directly so the result does not depend on perl or on a scriptlet running.
    """
    pamd = os.path.join(root, "etc", "pam.d")
    os.makedirs(pamd, exist_ok=True)

    common = {
        "common-auth": """\
# Generated by NETHOS (equivalent to Debian's pam-auth-update output)
auth	[success=1 default=ignore]	pam_unix.so nullok
auth	requisite			pam_deny.so
auth	required			pam_permit.so
""",
        "common-account": """\
# Generated by NETHOS
account	[success=1 new_authtok_reqd=done default=ignore]	pam_unix.so
account	requisite			pam_deny.so
account	required			pam_permit.so
""",
        "common-session": """\
# Generated by NETHOS
session	[default=1]			pam_permit.so
session	requisite			pam_deny.so
session	required			pam_permit.so
session	optional			pam_umask.so
session	required			pam_unix.so
session	optional			pam_systemd.so
""",
        "common-session-noninteractive": """\
# Generated by NETHOS
session	[default=1]			pam_permit.so
session	requisite			pam_deny.so
session	required			pam_permit.so
session	optional			pam_umask.so
session	required			pam_unix.so
""",
        "common-password": """\
# Generated by NETHOS
password	[success=1 default=ignore]	pam_unix.so obscure yescrypt
password	requisite			pam_deny.so
password	required			pam_permit.so
""",
    }
    for name, body in common.items():
        path = os.path.join(pamd, name)
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write(body)

    # `su` and `sudo` have their own stacks, and the packages do ship those --
    # but only write ours if the package did not, so a real one always wins.
    fallbacks = {
        "su": "auth       sufficient pam_rootok.so\n"
              "@include common-auth\n@include common-account\n"
              "@include common-session\n",
        "sudo": "@include common-auth\n@include common-account\n"
                "@include common-session-noninteractive\n",
        "login": "auth       requisite  pam_nologin.so\n"
                 "@include common-auth\n@include common-account\n"
                 "@include common-session\n@include common-password\n",
        "other": "@include common-auth\n@include common-account\n"
                 "@include common-password\n@include common-session\n",
    }
    for name, body in fallbacks.items():
        path = os.path.join(pamd, name)
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write(body)


def setup_sudo(root: str) -> None:
    """wheel may sudo, and sudo/su get the setuid bit they cannot work without."""
    etc = os.path.join(root, "etc")
    sudoers = os.path.join(etc, "sudoers")
    with open(sudoers, "w") as fh:
        fh.write(
            "# NETHOS\n"
            "Defaults env_reset\n"
            "Defaults secure_path=\"/usr/bin:/usr/local/bin\"\n"
            "Defaults pwfeedback\n\n"
            "root  ALL=(ALL:ALL) ALL\n"
            "# The Arch convention: administrators are in wheel.\n"
            "%wheel ALL=(ALL:ALL) ALL\n\n"
            "@includedir /etc/sudoers.d\n")
    os.chmod(sudoers, 0o440)
    os.makedirs(os.path.join(etc, "sudoers.d"), exist_ok=True)
    os.chmod(os.path.join(etc, "sudoers.d"), 0o750)

    # These are the two binaries that must be setuid-root, and the two people
    # most often trip over: without this, `sudo` says "must be owned by uid 0"
    # and `su -` says "cannot set user id".
    for rel in ("usr/bin/sudo", "usr/bin/su", "usr/bin/newgrp",
                "usr/bin/passwd", "usr/bin/chsh", "usr/bin/mount",
                "usr/bin/umount"):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            if os.geteuid() == 0:
                os.chown(path, 0, 0)
            os.chmod(path, 0o4755)


def setup_etc(root: str, hostname: str) -> None:
    etc = os.path.join(root, "etc")
    writes = {
        "hostname": hostname + "\n",
        "hosts": ("127.0.0.1\tlocalhost\n::1\t\tlocalhost\n"
                  f"127.0.1.1\t{hostname}\n"),
        "shells": "/bin/sh\n/bin/bash\n/usr/bin/bash\n",
        "resolv.conf": "nameserver 1.1.1.1\nnameserver 9.9.9.9\n",
        "fstab": "# <file system> <dir> <type> <options> <dump> <pass>\n",
        "os-release": ('NAME="NETHOS"\nPRETTY_NAME="NETHOS"\nID=nethos\n'
                       'ID_LIKE=debian\nBUILD_ID=rolling\nANSI_COLOR="0;36"\n'
                       'HOME_URL="https://github.com/Caleb22589/nethos"\n'),
        # Debian's base-files ships /etc/issue, so without this the login
        # banner cheerfully announces Debian on a system that has no dpkg,
        # no apt and a filesystem laid out nothing like Debian's.
        # Deliberately blank. A system that announces itself is configured at
        # the user; one that stays quiet is used by them. The identity is meant
        # to be legible from the shape of the interface, not from a banner.
        # See docs/DESIGN.md, rule 8.
        "issue": "\n",
        "issue.net": "NETHOS %h\n",
        "motd": "",
        "login.defs": ("UID_MIN 1000\nUID_MAX 60000\nGID_MIN 1000\nGID_MAX 60000\n"
                       "ENCRYPT_METHOD SHA512\nUMASK 022\nCREATE_HOME yes\n"),
        "profile": ('export PATH="/usr/bin:/usr/local/bin"\n'
                    "export EDITOR=nano\numask 022\n"),
        # pam_env reads these on every login and complains to the journal for
        # each one it cannot open. Harmless, but it is noise sitting on top of
        # every real login problem, which is exactly where you do not want it.
        "environment": 'PATH="/usr/bin:/usr/local/bin"\nLANG="C.UTF-8"\n',
    }
    for name, body in writes.items():
        with open(os.path.join(etc, name), "w") as fh:
            fh.write(body)

    os.makedirs(os.path.join(etc, "default"), exist_ok=True)
    with open(os.path.join(etc, "default", "locale"), "w") as fh:
        fh.write('LANG="C.UTF-8"\n')

    # ld.so must be told where libraries live now that the triplet is gone.
    os.makedirs(os.path.join(etc, "ld.so.conf.d"), exist_ok=True)
    with open(os.path.join(etc, "ld.so.conf"), "w") as fh:
        fh.write("/usr/lib\n/usr/local/lib\ninclude /etc/ld.so.conf.d/*.conf\n")


# ---------------------------------------------------------------------------
# conversion, in parallel
# ---------------------------------------------------------------------------

def _convert_one(job: tuple[str, str]) -> tuple[str | None, str | None]:
    """Convert one .deb. Returns (npk path, None) or (None, reason).

    Top level and taking a single tuple so it can be handed to a process pool:
    a closure or a bound method cannot be sent to a worker.
    """
    path, npks = job
    try:
        return convert_deb(path, npks, layout="arch"), None
    except Exception as exc:                  # noqa: BLE001 - one bad package
        return None, f"{os.path.basename(path)}: {exc}"


def _convert_many(paths: list[str], npks: str):
    """Convert every .deb, using every core, yielding results as they land.

    Falls back to converting in this process if a pool cannot be started. The
    build taking longer is a far better outcome than the build not happening,
    and process pools have more ways to fail than the work itself does.
    """
    workers = min(len(paths), os.cpu_count() or 4)
    jobs = [(p, npks) for p in paths]
    if workers > 1:
        try:
            # fork, so the workers inherit this module already imported rather
            # than re-executing a script that was run by path.
            ctx = multiprocessing.get_context("fork")
            with futures.ProcessPoolExecutor(max_workers=workers,
                                             mp_context=ctx) as pool:
                yield from pool.map(_convert_one, jobs, chunksize=4)
            return
        except (OSError, ValueError, ImportError,
                futures.process.BrokenProcessPool) as exc:
            say(f"  (parallel conversion unavailable: {exc}; using one core)")
    for job in jobs:
        yield _convert_one(job)


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def bootstrap(root: str, sets: list[str], arch: str, username: str,
              password: str, root_password: str, hostname: str,
              work: str, mirror: str, suite: str, keep: bool = False) -> None:
    debs = os.path.join(work, "debs")
    npks = os.path.join(work, "packages")
    os.makedirs(work, exist_ok=True)

    say(f"\n== Debian {suite}/{arch} index ==")
    archive = DebianArchive(mirror, suite, arch, cache=work)
    archive.load()

    seeds = []
    for name in sets:
        if name not in SETS:
            raise NpkgError(f"unknown set '{name}' (have: {', '.join(SETS)})")
        seeds += [s.format(arch=arch) for s in SETS[name]]

    if "base" in sets:
        essential = archive.base_seeds()
        say(f"  base: {len(essential)} essential/required packages from Debian")
        seeds += essential

    say(f"\n== resolving {len(seeds)} seed packages ==")
    resolved = archive.resolve(seeds)
    total = sum(int(f.get("Size", 0)) for f in resolved)
    say(f"  {len(resolved)} packages, {total/1e6:.0f} MB to download")

    # Downloads are latency-bound: several hundred small files, each paying a
    # connection setup, against a mirror that is perfectly happy to serve them
    # at once. Threads rather than processes because none of this is our CPU.
    say("\n== downloading ==")
    paths: list[str] = [""] * len(resolved)
    done = 0
    with futures.ThreadPoolExecutor(max_workers=16) as pool:
        jobs = {pool.submit(archive.download, f, debs): i
                for i, f in enumerate(resolved)}
        for job in futures.as_completed(jobs):
            paths[jobs[job]] = job.result()   # a failed download still raises
            done += 1
            if done % 50 == 0 or done == len(resolved):
                say(f"  {done}/{len(resolved)}")

    # Conversion is the opposite: xz and zstd decompression of every data.tar,
    # which is pure CPU and pinned to one core. Processes, because the work is
    # inside C decompressors and Python's own tar loop.
    say("\n== converting to npkg, Arch layout ==")
    npk_paths, done = [], 0
    for npk, err in _convert_many(paths, npks):
        if err:
            say(f"  skipped {err}")
        else:
            npk_paths.append(npk)
        done += 1
        if done % 50 == 0 or done == len(paths):
            say(f"  {done}/{len(paths)}")
    build_index(npks)

    say("\n== building the root ==")
    make_skeleton(root)

    db = Database(root)
    tx = Transaction(db, [], verbose=False)
    installed = 0
    # Installed from what we just converted, not from whatever is lying in the
    # directory. With the package cache persisting across builds, a listdir
    # here would install last week's leftovers alongside this build's set --
    # including packages that have since been dropped or superseded.
    for path in sorted(npk_paths):
        try:
            tx.install_files([path])
            installed += 1
        except NpkgError as exc:
            say(f"  {os.path.basename(path)}: {exc}")
    say(f"  installed {installed} packages")

    # Configure the updater, so `nethos-update` works on a fresh install
    # without being told where the system came from. Nearly every change --
    # the shell, nethosd, nethos-view, the sway config, everything in bin --
    # is files, and files can be replaced on a running machine in seconds. A
    # rebuild is only needed for the kernel, the package set, or the boot path.
    os.makedirs(os.path.join(root, "etc/nethos"), exist_ok=True)
    with open(os.path.join(root, "etc/nethos/update.conf"), "w") as fh:
        fh.write("# Where nethos-update pulls from.\n"
                 'REPO_URL="https://github.com/Caleb22589/nethos.git"\n'
                 'BRANCH="main"\n')

    # Tell the installed npkg which release this system came from, so `npkg
    # fetch` pulls from the same one rather than from a hardcoded default.
    os.makedirs(os.path.join(root, "etc/npkg"), exist_ok=True)
    with open(os.path.join(root, "etc/npkg/suite"), "w") as fh:
        fh.write(suite + "\n")

    say("\n== users, sudo, /etc ==")
    setup_etc(root, hostname)
    setup_users(root, username, password, root_password)
    setup_pam(root)
    setup_sudo(root)
    setup_network(root)
    install_npkg(root)

    if "desktop" in sets:
        payload = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "payload")
        if os.path.isdir(payload):
            say("\n== installing the NETHOS desktop ==")
            install_desktop(root, payload, username)
            for unit in ("seatd.service", "NetworkManager.service",
                         "nethos-growroot.service"):
                try:
                    from npkg_service import enable as enable_unit  # noqa: PLC0415
                    made = enable_unit(root, unit)
                    if made:
                        say(f"  enabled {unit}")
                except Exception:
                    pass
        else:
            say(f"  ! payload not found at {payload}; shell not installed")

    if os.geteuid() != 0:
        say("\n  ! not running as root: file ownership and setuid bits were "
              "not applied.\n    sudo and su will refuse to work until this is "
              "built as root.")

    if not keep:
        shutil.rmtree(debs, ignore_errors=True)

    say(f"\nDone: {root}")
    say(f"  user {username} (wheel), root account, /home/{username}")
    say(f"  npkg --root {root} list")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="npkg-bootstrap",
        description="build an Arch-shaped root filesystem from Debian packages")
    parser.add_argument("root")
    parser.add_argument("--set", dest="sets", action="append",
                        help=f"package set, repeatable ({', '.join(SETS)})")
    parser.add_argument("--arch", default="arm64", help="Debian architecture")
    parser.add_argument("--suite", default=SUITE)
    parser.add_argument("--mirror", default=MIRROR)
    parser.add_argument("--user", default="neth")
    parser.add_argument("--password", default="nethos")
    parser.add_argument("--root-password", default="nethos")
    parser.add_argument("--hostname", default="nethos")
    parser.add_argument("--work", default="./bootstrap-work")
    parser.add_argument("--keep-debs", action="store_true")
    args = parser.parse_args(argv)

    try:
        bootstrap(os.path.abspath(args.root), args.sets or ["base"], args.arch,
                  args.user, args.password, args.root_password, args.hostname,
                  os.path.abspath(args.work), args.mirror, args.suite,
                  keep=args.keep_debs)
    except NpkgError as exc:
        say(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
