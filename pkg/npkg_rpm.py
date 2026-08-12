#!/usr/bin/env python3
"""
npkg_rpm — read RPM packages without rpm.

An .rpm is a lead, a signature header, a metadata header and then a compressed
cpio archive of the files. The headers are a simple binary format: a magic, a
count of index entries, and a data blob the entries point into. That is enough
to get names, versions, dependencies and the payload out with nothing but the
standard library.

    npkg convert firefox.rpm

Read the honesty section in npkg_convert before mixing distributions. RPM makes
it worse rather than better, because Fedora expresses most dependencies as
capabilities rather than package names:

    Requires: libc.so.6(GLIBC_2.34)(64bit)
    Requires: /usr/bin/sh

Those are satisfied by *files and sonames*, not by a package called libc6 or
glibc. A converted RPM therefore asks for things no Debian package declares,
even though the underlying library is sitting right there. Requirements that
look like capabilities are dropped rather than translated into fiction, and
the manifest records what was dropped.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile

from npkg import Manifest, Package, NpkgError

# Header tags we care about (rpmtag.h).
TAG = {
    1000: "name", 1001: "version", 1002: "release", 1004: "summary",
    1005: "description", 1014: "licence", 1020: "url", 1022: "arch",
    1009: "size", 1047: "provides", 1049: "requires", 1054: "conflicts",
    1090: "obsoletes", 1124: "payload_format", 1125: "payload_compressor",
}

TYPE_STRING, TYPE_STRING_ARRAY, TYPE_I18NSTRING = 6, 8, 9
TYPE_INT16, TYPE_INT32 = 3, 4


def _read_header(fh, pad: bool = False) -> tuple[dict, int]:
    """Parse one RPM header section.

    `pad` matters and is easy to get wrong: the *signature* header is padded to
    an 8-byte boundary, the main header is not -- the payload starts the byte
    after it. Padding both walks several bytes into the payload and its magic
    stops being recognisable, which surfaces as "unsupported format" from
    whichever decompressor you hand it to.
    """
    magic = fh.read(8)
    if len(magic) < 8 or magic[:3] != b"\x8e\xad\xe8":
        raise NpkgError("not an RPM header")
    count, size = struct.unpack(">II", fh.read(8))
    index = fh.read(16 * count)
    store = fh.read(size)

    tags: dict = {}
    for i in range(count):
        tag, typ, offset, num = struct.unpack(">IIII", index[i * 16:(i + 1) * 16])
        name = TAG.get(tag)
        if name is None:
            continue
        if typ in (TYPE_STRING, TYPE_I18NSTRING, TYPE_STRING_ARRAY):
            values, pos = [], offset
            for _ in range(num):
                end = store.find(b"\x00", pos)
                values.append(store[pos:end].decode("utf-8", "replace"))
                pos = end + 1
            tags[name] = values if typ == TYPE_STRING_ARRAY else values[0]
        elif typ == TYPE_INT32:
            tags[name] = struct.unpack(">I", store[offset:offset + 4])[0]
        elif typ == TYPE_INT16:
            tags[name] = struct.unpack(">H", store[offset:offset + 2])[0]

    if pad:
        pos = fh.tell()
        if pos % 8:
            fh.read(8 - (pos % 8))
    return tags, fh.tell()


def read_rpm(path: str) -> tuple[dict, bytes]:
    """Return the metadata header and the still-compressed payload."""
    with open(path, "rb") as fh:
        lead = fh.read(96)
        if len(lead) < 96 or lead[:4] != b"\xed\xab\xee\xdb":
            raise NpkgError(f"{path}: not an RPM (bad lead magic)")
        _read_header(fh, pad=True)            # signature header, discarded
        tags, _ = _read_header(fh)            # metadata; payload follows it
        return tags, fh.read()


def _decompress(payload: bytes, compressor: str) -> bytes:
    if compressor in ("gzip", "", None):
        import gzip
        return gzip.decompress(payload)
    if compressor == "xz":
        import lzma
        return lzma.decompress(payload)
    if compressor == "bzip2":
        import bz2
        return bz2.decompress(payload)
    if compressor == "zstd":
        if shutil.which("zstd"):
            result = subprocess.run(["zstd", "-dc"], input=payload,
                                    capture_output=True)
            if result.returncode == 0:
                return result.stdout
        try:
            from compression import zstd       # Python >= 3.14
            return zstd.decompress(payload)
        except ImportError:
            pass
        raise NpkgError("zstd-compressed RPM and no zstd available")
    raise NpkgError(f"unsupported RPM payload compressor: {compressor}")


def extract_cpio(blob: bytes, dest: str) -> int:
    """Unpack a 'new ASCII' cpio archive, which is what RPM payloads are.

    Written out rather than shelling to cpio: the format is a 110-byte ASCII
    header of hex fields, and depending on a cpio binary would put a tool
    between us and reading our own packages.
    """
    pos, count = 0, 0
    while pos + 110 <= len(blob):
        header = blob[pos:pos + 110]
        if header[:6] not in (b"070701", b"070702"):
            break
        fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        mode, uid, gid, nlink, mtime, filesize = (
            fields[1], fields[2], fields[3], fields[4], fields[5], fields[6])
        namesize = fields[11]

        pos += 110
        name = blob[pos:pos + namesize - 1].decode("utf-8", "replace")
        pos += namesize
        pos += (4 - (pos % 4)) % 4            # header+name padded to 4 bytes

        data = blob[pos:pos + filesize]
        pos += filesize
        pos += (4 - (pos % 4)) % 4

        if name == "TRAILER!!!":
            break

        rel = name.lstrip("./")
        if not rel:
            continue
        target = os.path.normpath(os.path.join(dest, rel))
        if not target.startswith(os.path.realpath(dest)) and \
                not target.startswith(dest):
            continue                          # never escape the staging tree

        kind = mode & 0o170000
        if kind == 0o040000:                  # directory
            os.makedirs(target, exist_ok=True)
        elif kind == 0o120000:                # symlink
            os.makedirs(os.path.dirname(target), exist_ok=True)
            link = data.decode("utf-8", "replace").rstrip("\x00")
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(link, target)
            count += 1
        else:                                 # regular file
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            os.chmod(target, mode & 0o7777)
            count += 1
    return count


# A capability rather than a package name: sonames, file paths, rpmlib
# features. These cannot be satisfied by a package name in our world.
CAPABILITY = re.compile(r"^(/|rpmlib\(|config\(|.*\.so[.0-9]*(\(|$))")


def rpm_requirements(values) -> tuple[list[str], list[str]]:
    """Split Requires into things we can use and capabilities we cannot."""
    usable, dropped = [], []
    for raw in values or []:
        item = raw.strip()
        if not item:
            continue
        if CAPABILITY.match(item):
            dropped.append(item)
        else:
            usable.append(item.split()[0])
    return usable, dropped


def convert_rpm(path: str, outdir: str, layout: str = "native") -> str:
    from npkg_convert import relayout          # noqa: PLC0415

    tags, payload = read_rpm(path)
    if tags.get("payload_format", "cpio") not in ("cpio", ""):
        raise NpkgError(f"{path}: unsupported payload format "
                        f"{tags.get('payload_format')}")

    blob = _decompress(payload, tags.get("payload_compressor", "gzip"))
    staging = tempfile.mkdtemp(prefix="npkg-rpm-")
    try:
        extract_cpio(blob, staging)
        if layout == "arch":
            relayout(staging)

        requires, dropped = rpm_requirements(tags.get("requires"))
        provides, _ = rpm_requirements(tags.get("provides"))
        conflicts, _ = rpm_requirements(tags.get("conflicts"))
        obsoletes, _ = rpm_requirements(tags.get("obsoletes"))

        release = str(tags.get("release", "1"))
        note = (f"\n\nConverted from rpm by npkg."
                f"\n{len(dropped)} capability requirements were dropped "
                f"(sonames and file paths RPM resolves by content, which have "
                f"no package-name equivalent here)." if dropped else
                "\n\nConverted from rpm by npkg.")

        manifest = Manifest(
            name=tags.get("name", "unknown"),
            version=str(tags.get("version", "0")),
            release=int(re.sub(r"\D.*$", "", release) or 1),
            arch=tags.get("arch", "noarch"),
            summary=tags.get("summary", ""),
            description=(tags.get("description", "") + note).strip(),
            url=tags.get("url", ""),
            licence=tags.get("licence", ""),
            depends=requires,
            provides=provides,
            conflicts=conflicts,
            replaces=obsoletes,
        )
        os.makedirs(outdir, exist_ok=True)
        out = os.path.join(outdir, f"{manifest.name}-{manifest.version}-"
                                   f"{manifest.release}.{manifest.arch}.npk")
        Package.create(manifest, staging, out)
        return out
    finally:
        shutil.rmtree(staging, ignore_errors=True)
