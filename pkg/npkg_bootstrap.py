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
import gzip
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

def say(*args):
    """print(), but visible immediately even through a pipe."""
    print(*args, flush=True)


MIRROR = "http://deb.debian.org/debian"
SUITE = "bookworm"

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
    ],
    "kernel": [
        # {arch} is substituted for the Debian architecture being built.
        "linux-image-{arch}",
        # Debian's arm64 kernel has virtio as modules, so the root device is
        # unreachable without an initramfs to load them first.
        "initramfs-tools", "busybox", "zstd",
        "grub-efi-{arch}-bin", "grub-common", "grub2-common", "efibootmgr",
    ],
    "net": ["network-manager", "wpasupplicant", "iw", "wireless-regdb"],
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
                 components=("main",), cache="."):
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

            deps = fields.get("Depends", "")
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
        if os.path.isfile(target):
            return target
        os.makedirs(outdir, exist_ok=True)
        url = f"{self.mirror}/{filename}"
        tmp = target + ".part"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        except (urllib.error.URLError, OSError) as exc:
            raise NpkgError(f"download failed: {url}: {exc}") from exc
        os.replace(tmp, target)
        return target


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
                       "BUILD_ID=rolling\nANSI_COLOR=\"0;36\"\n"),
        "login.defs": ("UID_MIN 1000\nUID_MAX 60000\nGID_MIN 1000\nGID_MAX 60000\n"
                       "ENCRYPT_METHOD SHA512\nUMASK 022\nCREATE_HOME yes\n"),
        "profile": ('export PATH="/usr/bin:/usr/local/bin"\n'
                    "export EDITOR=nano\numask 022\n"),
    }
    for name, body in writes.items():
        with open(os.path.join(etc, name), "w") as fh:
            fh.write(body)

    # ld.so must be told where libraries live now that the triplet is gone.
    os.makedirs(os.path.join(etc, "ld.so.conf.d"), exist_ok=True)
    with open(os.path.join(etc, "ld.so.conf"), "w") as fh:
        fh.write("/usr/lib\n/usr/local/lib\ninclude /etc/ld.so.conf.d/*.conf\n")


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

    say(f"\n== resolving {len(seeds)} seed packages ==")
    resolved = archive.resolve(seeds)
    total = sum(int(f.get("Size", 0)) for f in resolved)
    say(f"  {len(resolved)} packages, {total/1e6:.0f} MB to download")

    say("\n== downloading ==")
    paths = []
    for i, fields in enumerate(resolved, 1):
        paths.append(archive.download(fields, debs))
        if i % 25 == 0 or i == len(resolved):
            say(f"  {i}/{len(resolved)}")

    say("\n== converting to npkg, Arch layout ==")
    for i, path in enumerate(paths, 1):
        try:
            convert_deb(path, npks, layout="arch")
        except NpkgError as exc:
            say(f"  skipped {os.path.basename(path)}: {exc}")
        if i % 25 == 0 or i == len(paths):
            say(f"  {i}/{len(paths)}")
    build_index(npks)

    say("\n== building the root ==")
    make_skeleton(root)

    db = Database(root)
    tx = Transaction(db, [], verbose=False)
    installed = 0
    for name in sorted(os.listdir(npks)):
        if not name.endswith(".npk"):
            continue
        try:
            tx.install_files([os.path.join(npks, name)])
            installed += 1
        except NpkgError as exc:
            say(f"  {name}: {exc}")
    say(f"  installed {installed} packages")

    say("\n== users, sudo, /etc ==")
    setup_etc(root, hostname)
    setup_users(root, username, password, root_password)
    setup_sudo(root)

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
