#!/usr/bin/env python3
"""
npkg_convert — turn Arch and Debian packages into npkg packages.

This is the pragmatic answer to "where do 15,000 packages come from". Both
formats are, underneath, a tarball of files plus a small metadata blob:

    Arch    foo.pkg.tar.zst   tar  ->  .PKGINFO (key = value), then the files
    Debian  foo.deb           ar   ->  control.tar.* (RFC822 control)
                                       data.tar.*    (the files)

So conversion is mostly reading someone else's metadata dialect and writing
ours. Nothing is recompiled and nothing is patched.

    npkg convert foo.pkg.tar.zst
    npkg convert *.deb -o packages/
    npkg index packages/

What converts cleanly:
    file contents and layout, name, version, description, dependencies,
    provides/conflicts/replaces, installed size

What does NOT, and why you should pick one upstream and stay there:
    Package names differ between distributions -- Debian's `libssl3` is Arch's
    `openssl`. Dependencies are translated verbatim, so a converted Debian
    package asks for Debian names and a converted Arch package asks for Arch
    names. Mix the two and the solver will ask for packages that do not exist.
    Library sonames and file layouts differ too (/usr/lib vs /usr/lib64,
    multiarch paths like /usr/lib/x86_64-linux-gnu). Maintainer scriptlets are
    carried over only where they are plain shell.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from npkg import Manifest, Package, NpkgError, extract_all

# Metadata files that belong to the source format, not to the package payload.
ARCH_META = {".PKGINFO", ".MTREE", ".BUILDINFO", ".INSTALL", ".CHANGELOG"}


# ---------------------------------------------------------------------------
# compression helpers
# ---------------------------------------------------------------------------

def open_tar(path_or_fileobj) -> tarfile.TarFile:
    """Open a tar in whatever compression it happens to use.

    zstd is the awkward one: Python only learned it in 3.14, and Arch has used
    it for years. Fall back to the zstd binary when the stdlib cannot help.
    """
    if isinstance(path_or_fileobj, str):
        name = path_or_fileobj
        if name.endswith(".zst"):
            try:
                return tarfile.open(name, "r:zst")          # Python >= 3.14
            except (tarfile.CompressionError, ValueError, KeyError):
                return tarfile.open(fileobj=_zstd_decompress(name), mode="r:")
        return tarfile.open(name, "r:*")
    return tarfile.open(fileobj=path_or_fileobj, mode="r:*")


def _zstd_decompress(path: str) -> io.BytesIO:
    if not shutil.which("zstd"):
        raise NpkgError(
            "this package is zstd-compressed and this Python cannot read it.\n"
            "  Install the zstd tool (brew install zstd / pacman -S zstd), "
            "or run on Python 3.14+.")
    result = subprocess.run(["zstd", "-dc", path], capture_output=True)
    if result.returncode != 0:
        raise NpkgError(f"zstd failed on {path}: {result.stderr.decode()[:200]}")
    return io.BytesIO(result.stdout)


def _decompress_blob(blob: bytes, filename: str) -> io.BytesIO:
    if filename.endswith(".zst"):
        if shutil.which("zstd"):
            result = subprocess.run(["zstd", "-dc"], input=blob, capture_output=True)
            if result.returncode == 0:
                return io.BytesIO(result.stdout)
        try:
            from compression import zstd as _zstd          # Python >= 3.14
            return io.BytesIO(_zstd.decompress(blob))
        except ImportError:
            raise NpkgError("zstd-compressed .deb and no zstd available") from None
    return io.BytesIO(blob)


# ---------------------------------------------------------------------------
# Arch Linux
# ---------------------------------------------------------------------------

def parse_pkginfo(text: str) -> dict[str, list[str]]:
    """.PKGINFO is `key = value`, one per line, keys repeat for lists."""
    fields: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields.setdefault(key.strip(), []).append(value.strip())
    return fields


def arch_requirement(text: str) -> str:
    """Arch already uses our syntax; just drop descriptions from optdepends."""
    return text.split(":", 1)[0].strip()


def convert_arch(path: str, outdir: str, layout: str = "native") -> str:
    with open_tar(path) as tar:
        try:
            fh = tar.extractfile(".PKGINFO")
        except KeyError:
            fh = None
        if fh is None:
            raise NpkgError(f"{path}: no .PKGINFO — not an Arch package")
        fields = parse_pkginfo(fh.read().decode("utf-8", "replace"))

        staging = tempfile.mkdtemp(prefix="npkg-arch-")
        try:
            members = [m for m in tar.getmembers()
                       if m.name.lstrip("./").split("/")[0] not in ARCH_META
                       and m.name not in ARCH_META]
            extract_all(tar, staging, members)

            version_full = fields.get("pkgver", ["0"])[0]
            version, _, release = version_full.rpartition("-")
            if not version:
                version, release = version_full, "1"

            manifest = Manifest(
                name=fields.get("pkgname", ["unknown"])[0],
                version=version,
                release=int(release) if str(release).isdigit() else 1,
                arch=fields.get("arch", ["any"])[0],
                summary=fields.get("pkgdesc", [""])[0],
                url=fields.get("url", [""])[0],
                licence=", ".join(fields.get("license", [])),
                depends=[arch_requirement(d) for d in fields.get("depend", [])],
                optional=[arch_requirement(d) for d in fields.get("optdepend", [])],
                provides=[arch_requirement(d) for d in fields.get("provides", [])],
                conflicts=[arch_requirement(d) for d in fields.get("conflict", [])],
                replaces=[arch_requirement(d) for d in fields.get("replaces", [])],
            )
            return _write(manifest, staging, outdir, source="arch", layout=layout)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Debian
# ---------------------------------------------------------------------------

def read_ar(path: str) -> dict[str, bytes]:
    """Minimal `ar` reader.

    A .deb is an ar archive, and Python has no ar module — but the format is
    trivial: a magic line, then 60-byte headers each followed by the payload,
    padded to an even length.
    """
    entries: dict[str, bytes] = {}
    with open(path, "rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            raise NpkgError(f"{path}: not an ar archive — not a .deb")
        while True:
            header = fh.read(60)
            if len(header) < 60:
                break
            name = header[0:16].decode("ascii", "replace").strip()
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError:
                break
            data = fh.read(size)
            if size % 2:
                fh.read(1)                      # entries are 2-byte aligned
            entries[name.rstrip("/")] = data
    return entries


def parse_control(text: str) -> dict[str, str]:
    """RFC822-ish: `Field: value`, continuation lines start with a space."""
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


DEB_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9+.\-]*)(?::\w+)?\s*(?:\(([^)]*)\))?")


def deb_requirements(text: str) -> list[str]:
    """Translate a Debian dependency field into our requirement syntax.

    "libc6 (>= 2.34), foo | bar, baz:any" -> ["libc6>=2.34", "foo", "baz"]
    Alternatives collapse to the first option: npkg has no concept of "or",
    and the first is conventionally the preferred one.
    """
    out = []
    for clause in text.split(","):
        clause = clause.strip()
        if not clause:
            continue
        first = clause.split("|")[0].strip()
        match = DEB_REQ.match(first)
        if not match:
            continue
        name, constraint = match.group(1), (match.group(2) or "").strip()
        if constraint:
            op, _, version = constraint.partition(" ")
            op = {">>": ">", "<<": "<", "=": "=="}.get(op.strip(), op.strip())
            version = strip_epoch(version.strip())
            out.append(f"{name}{op}{version}" if version else name)
        else:
            out.append(name)
    return out


def strip_epoch(version: str) -> str:
    """Debian epochs ("1:2.3") have no equivalent here; drop them.

    Losing the epoch can only make a version compare as older, never newer,
    so the worst case is an unnecessary upgrade rather than a missed one.
    """
    return version.split(":", 1)[1] if ":" in version else version


def convert_deb(path: str, outdir: str, layout: str = "native",
                scripts: bool = False) -> str:
    entries = read_ar(path)

    control_name = next((n for n in entries if n.startswith("control.tar")), None)
    data_name = next((n for n in entries if n.startswith("data.tar")), None)
    if not control_name or not data_name:
        raise NpkgError(f"{path}: missing control.tar/data.tar — not a .deb")

    with tarfile.open(fileobj=_decompress_blob(entries[control_name], control_name),
                      mode="r:*") as tar:
        member = next((m for m in tar.getmembers()
                       if os.path.basename(m.name) == "control"), None)
        if member is None:
            raise NpkgError(f"{path}: no control file")
        fields = parse_control(tar.extractfile(member).read().decode("utf-8", "replace"))
        hooks = {}
        for hook in ("postinst", "prerm") if scripts else ():
            m = next((m for m in tar.getmembers()
                      if os.path.basename(m.name) == hook), None)
            if m:
                blob = tar.extractfile(m).read().decode("utf-8", "replace")
                # Only carry over plain shell; anything else is Debian-specific
                # machinery that will not mean the same thing here.
                if blob.lstrip().startswith("#!/bin/sh"):
                    hooks[hook] = blob

    staging = tempfile.mkdtemp(prefix="npkg-deb-")
    try:
        with tarfile.open(fileobj=_decompress_blob(entries[data_name], data_name),
                          mode="r:*") as tar:
            extract_all(tar, staging)

        version_full = strip_epoch(fields.get("Version", "0"))
        version, _, release = version_full.rpartition("-")
        if not version:
            version, release = version_full, "1"

        description = fields.get("Description", "")
        summary, _, body = description.partition("\n")

        manifest = Manifest(
            name=fields.get("Package", "unknown"),
            version=version,
            release=int(release) if str(release).isdigit() else 1,
            arch=fields.get("Architecture", "any"),
            summary=summary.strip(),
            description=body.strip(),
            url=fields.get("Homepage", ""),
            licence="",                      # .deb keeps licences in a copyright file
            # Pre-Depends are dependencies too; npkg has no separate notion
            # of "must be configured first", so they merge into depends.
            depends=deb_requirements(", ".join(filter(None, [
                fields.get("Pre-Depends", ""), fields.get("Depends", "")]))),
            optional=deb_requirements(fields.get("Recommends", "")),
            provides=deb_requirements(fields.get("Provides", "")),
            conflicts=deb_requirements(fields.get("Conflicts", "")),
            replaces=deb_requirements(fields.get("Replaces", "")),
            post_install=hooks.get("postinst", ""),
            pre_remove=hooks.get("prerm", ""),
        )
        return _write(manifest, staging, outdir, source="deb", layout=layout)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# layout normalisation
# ---------------------------------------------------------------------------

# Debian puts libraries under a per-architecture triplet directory and keeps
# sbin separate. Arch does neither: everything lives in /usr/bin and /usr/lib.
TRIPLET = re.compile(
    r"^(usr/)?lib(64)?/"
    r"(?:aarch64|arm|x86_64|i386|riscv64|powerpc64le|s390x)-[a-z0-9]+-[a-z]+[a-z0-9]*/")

# Order matters: the triplet rule runs first, then these prefix moves.
PREFIX_MOVES = (
    ("usr/sbin/", "usr/bin/"),
    ("sbin/", "usr/bin/"),
    ("bin/", "usr/bin/"),
    ("lib64/", "usr/lib/"),
    ("lib/", "usr/lib/"),
)


def arch_path(path: str) -> str:
    """Rewrite one Debian path into the Arch layout.

    usr/lib/aarch64-linux-gnu/libc.so.6  ->  usr/lib/libc.so.6
    sbin/ldconfig                        ->  usr/bin/ldconfig
    usr/sbin/sudo                        ->  usr/bin/sudo

    Nothing outside /bin, /sbin and /lib is touched: /etc, /usr/share and
    /var mean the same thing in both distributions.
    """
    path = path.lstrip("./").lstrip("/")
    path = TRIPLET.sub("usr/lib/", path)
    for old, new in PREFIX_MOVES:
        if path.startswith(old):
            return new + path[len(old):]
        if path.startswith("usr/" + old) and old in ("bin/", "lib/"):
            return path                       # already usr/bin or usr/lib
    return path


def relayout(staging: str) -> int:
    """Move a staged package tree into the Arch layout in place."""
    moved = 0
    for base, _dirs, names in os.walk(staging, topdown=False):
        for name in names:
            src = os.path.join(base, name)
            rel = os.path.relpath(src, staging)
            new = arch_path(rel)
            if new == rel:
                continue
            dst = os.path.join(staging, new)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst) or os.path.islink(dst):
                # Same file arriving by two routes (a package shipping both
                # lib/foo and usr/lib/foo). Keep the first; they are identical.
                os.remove(src)
                continue
            os.replace(src, dst)
            moved += 1

    # Prune the directories the move emptied.
    for base, dirs, _names in os.walk(staging, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(base, d))
            except OSError:
                pass
    return moved


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------

def _write(manifest: Manifest, staging: str, outdir: str, source: str,
           layout: str = "native") -> str:
    # Debian data tarballs are rooted at "./"; flatten so paths match Arch's.
    inner = os.path.join(staging, ".")
    if os.path.isdir(inner) and os.listdir(staging) == ["."]:
        staging = inner

    if layout == "arch":
        moved = relayout(staging)
        if moved:
            manifest.description += f"\nRelaid out for the Arch layout ({moved} paths)."

    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"{manifest.name}-{manifest.version}-"
                               f"{manifest.release}.{manifest.arch}.npk")
    manifest.description = (manifest.description +
                            f"\n\nConverted from {source} by npkg.").strip()
    Package.create(manifest, staging, out)
    return out


def convert(path: str, outdir: str = "packages", layout: str = "native",
            scripts: bool = False) -> str:
    if path.endswith(".deb"):
        return convert_deb(path, outdir, layout, scripts)
    if ".pkg.tar" in path:
        return convert_arch(path, outdir, layout)
    raise NpkgError(f"{path}: unrecognised package (expected .deb or .pkg.tar.*)")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="npkg-convert",
        description="convert Arch (.pkg.tar.*) and Debian (.deb) packages to npkg")
    parser.add_argument("packages", nargs="+")
    parser.add_argument("-o", "--output", default="packages")
    parser.add_argument("--layout", choices=("native", "arch"), default="native",
                        help="'arch' merges sbin into bin and flattens Debian's "
                             "multiarch lib directories")
    args = parser.parse_args(argv)

    failures = 0
    for path in args.packages:
        try:
            out = convert(path, args.output, args.layout)
            print(f"{os.path.basename(path)}  ->  {os.path.basename(out)}")
        except NpkgError as exc:
            print(f"error: {path}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
