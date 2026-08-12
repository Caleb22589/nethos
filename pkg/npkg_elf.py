#!/usr/bin/env python3
"""
npkg_elf — read what a binary provides and needs, without readelf.

Package names differ between distributions; sonames do not. Debian's libssl3,
Arch's openssl and Fedora's openssl-libs all install a file that calls itself
libssl.so.3, and every binary linked against it asks for exactly that string.
So the portable way to answer "is this dependency satisfied" is to look at the
libraries themselves rather than at what somebody decided to call the package.

    soname("/usr/lib/libssl.so.3")   -> "libssl.so.3"       what it provides
    needed("/usr/bin/curl")          -> ["libcurl.so.4", …] what it requires

Both come out of the ELF .dynamic section: DT_SONAME for the first, DT_NEEDED
for the second. Parsing it is a header, a program header table and a walk of
tagged pairs — small enough to do here rather than depend on binutils being
installed on a system we are still building.
"""

from __future__ import annotations

import os
import struct

ELF_MAGIC = b"\x7fELF"

PT_LOAD, PT_DYNAMIC = 1, 2
DT_NULL, DT_NEEDED, DT_STRTAB, DT_SONAME, DT_STRSZ = 0, 1, 5, 14, 10


class ElfError(Exception):
    pass


class Elf:
    """Just enough ELF to reach the dynamic section.

    Reads slices rather than whole files: a package set with Chromium in it has
    binaries in the hundreds of megabytes, and we only ever want the header,
    the program headers and one segment.
    """

    def __init__(self, fh):
        self.fh = fh
        head = self._at(0, 64)
        if len(head) < 64 or head[:4] != ELF_MAGIC:
            raise ElfError("not an ELF file")
        self.data = head
        self.is64 = head[4] == 2
        self.little = head[5] == 1
        self.end = "<" if self.little else ">"

        if self.is64:
            self.phoff = self._int(0x20, 8)
            self.phentsize = self._int(0x36, 2)
            self.phnum = self._int(0x38, 2)
        else:
            self.phoff = self._int(0x1C, 4)
            self.phentsize = self._int(0x2A, 2)
            self.phnum = self._int(0x2C, 2)

        self.segments = list(self._program_headers())

    def _at(self, offset: int, length: int) -> bytes:
        try:
            self.fh.seek(offset)
            return self.fh.read(length)
        except OSError:
            return b""

    def _int(self, offset: int, size: int, buf: bytes = b"", base: int = 0) -> int:
        fmt = {1: "B", 2: "H", 4: "I", 8: "Q"}[size]
        if buf:
            return struct.unpack_from(self.end + fmt, buf, offset - base)[0]
        chunk = self._at(offset, size)
        if len(chunk) < size:
            raise ElfError("truncated")
        return struct.unpack(self.end + fmt, chunk)[0]

    def _program_headers(self):
        table = self._at(self.phoff, self.phentsize * self.phnum)
        for i in range(self.phnum):
            base = self.phoff + i * self.phentsize
            if (i + 1) * self.phentsize > len(table):
                return
            self.data = table
            self._base = self.phoff
            if self.is64:
                p_type = self._int(base, 4, table, self.phoff)
                p_offset = self._int(base + 0x08, 8, table, self.phoff)
                p_vaddr = self._int(base + 0x10, 8, table, self.phoff)
                p_filesz = self._int(base + 0x20, 8, table, self.phoff)
            else:
                p_type = self._int(base, 4, table, self.phoff)
                p_offset = self._int(base + 0x04, 4, table, self.phoff)
                p_vaddr = self._int(base + 0x08, 4, table, self.phoff)
                p_filesz = self._int(base + 0x10, 4, table, self.phoff)
            yield {"type": p_type, "offset": p_offset,
                   "vaddr": p_vaddr, "filesz": p_filesz}

    def vaddr_to_offset(self, addr: int) -> int | None:
        """Dynamic entries point at virtual addresses; we have a file."""
        for seg in self.segments:
            if seg["type"] == PT_LOAD and \
                    seg["vaddr"] <= addr < seg["vaddr"] + seg["filesz"]:
                return seg["offset"] + (addr - seg["vaddr"])
        return None

    def dynamic(self) -> list[tuple[int, int]]:
        for seg in self.segments:
            if seg["type"] != PT_DYNAMIC:
                continue
            step = 16 if self.is64 else 8
            size = 8 if self.is64 else 4
            blob = self._at(seg["offset"], seg["filesz"])
            entries, pos = [], 0
            while pos + step <= len(blob):
                tag = self._int(pos, size, blob, 0)
                val = self._int(pos + size, size, blob, 0)
                if tag == DT_NULL:
                    break
                entries.append((tag, val))
                pos += step
            return entries
        return []

    def _strings(self):
        entries = dict(self.dynamic())
        strtab = entries.get(DT_STRTAB)
        if strtab is None:
            return None, 0
        offset = self.vaddr_to_offset(strtab)
        if offset is None:
            return None, 0
        return offset, entries.get(DT_STRSZ, 0)

    def _string_at(self, base: int, index: int) -> str:
        # Sonames are short; 512 bytes is plenty and bounds the read.
        chunk = self._at(base + index, 512)
        end = chunk.find(b"\x00")
        return chunk[:end if end >= 0 else len(chunk)].decode("utf-8", "replace")

    def soname(self) -> str | None:
        base, _ = self._strings()
        if base is None:
            return None
        for tag, val in self.dynamic():
            if tag == DT_SONAME:
                return self._string_at(base, val) or None
        return None

    def needed(self) -> list[str]:
        base, _ = self._strings()
        if base is None:
            return []
        out = []
        for tag, val in self.dynamic():
            if tag == DT_NEEDED:
                name = self._string_at(base, val)
                if name:
                    out.append(name)
        return out


def _load(path: str) -> Elf | None:
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        fh = open(path, "rb")
    except OSError:
        return None
    try:
        if fh.read(4) != ELF_MAGIC:
            fh.close()
            return None                       # not ELF; nothing to say about it
        return Elf(fh)
    except (OSError, ElfError, struct.error):
        fh.close()
        return None


def soname(path: str) -> str | None:
    elf = _load(path)
    if elf is None:
        return None
    try:
        return elf.soname()
    finally:
        elf.fh.close()


def needed(path: str) -> list[str]:
    elf = _load(path)
    if elf is None:
        return []
    try:
        return elf.needed()
    finally:
        elf.fh.close()


def scan(root: str, paths) -> tuple[dict[str, str], set[str]]:
    """Index a package's files.

    Returns the sonames it provides (mapped to the file that carries them) and
    the sonames its binaries require. A library that ships without a DT_SONAME
    is indexed under its filename instead, which is what the loader would use
    anyway.
    """
    provides: dict[str, str] = {}
    requires: set[str] = set()
    for rel in paths:
        full = os.path.join(root, rel.lstrip("/"))
        elf = _load(full)
        if elf is None:
            continue
        try:
            name = elf.soname()
            base = os.path.basename(rel)
            if name:
                provides[name] = "/" + rel.lstrip("/")
            elif ".so" in base:
                provides[base] = "/" + rel.lstrip("/")
            requires.update(elf.needed())
        finally:
            elf.fh.close()
    return provides, requires
