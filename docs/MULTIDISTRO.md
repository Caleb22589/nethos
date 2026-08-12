# Packages from every distribution

npkg reads three package formats:

| Distribution | Format | Container | Metadata |
| --- | --- | --- | --- |
| Debian, Ubuntu, Mint | `.deb` | `ar` | `control.tar` — RFC822 |
| Arch, Manjaro | `.pkg.tar.zst` | tar | `.PKGINFO` — `key = value` |
| Fedora, RHEL, Rocky, openSUSE | `.rpm` | lead + headers + cpio | binary header |

```bash
npkg convert firefox.deb
npkg convert firefox.pkg.tar.zst
npkg convert firefox.rpm
```

All three are verified against real packages off real mirrors, not fixtures.

## You asked whether this is stupid. It is half stupid.

**Reading every format: sensible.** They are all a metadata blob and an archive
of files. Nothing is recompiled and the conversion is lossless in the ways that
matter.

**Running packages from several distributions at once: mostly not.** Not
because of the formats, but because of what the metadata *means*.

### Dependencies name packages that do not exist elsewhere

The same library, three names:

```
Debian   libssl3        Arch  openssl        Fedora  openssl-libs
Debian   libc6          Arch  glibc          Fedora  glibc
```

Dependencies are translated verbatim, because inventing a mapping would be
guessing. So a converted Fedora package asks for `openssl-libs`, and if your
system was built from Debian it has `libssl3` — the file is there, the name is
not, and the solver refuses.

### RPM asks for capabilities, not packages

Fedora mostly does not name packages at all:

```
Requires: libc.so.6(GLIBC_2.34)(64bit)
Requires: /usr/bin/sh
Requires: rpmlib(CompressedFileNames)
```

These are satisfied by *files and sonames*. npkg has no capability index, so
they are dropped rather than turned into fiction, and the manifest records how
many went. `tree` from Rocky Linux dropped ten. In practice that package still
runs, because the libraries genuinely are present — but the checking is gone,
and a package with a real unmet requirement will now install and then fail at
runtime instead of refusing.

### File layouts disagree

```
Debian   /usr/lib/aarch64-linux-gnu/libfoo.so
Arch     /usr/lib/libfoo.so
Fedora   /usr/lib64/libfoo.so
```

`--layout arch` flattens Debian's multiarch directories and merges `sbin` into
`bin`, which makes converted packages agree with each other. It also means the
occasional collision: `tar` and `cpio` both ship `/usr/sbin/rmt`, which becomes
the same path once sbin is merged, and npkg refuses the second one rather than
letting it overwrite the first.

## What actually works

**One upstream, all the way down.** Build the system from Debian and fetch from
Debian. That is what `npkg fetch` does and it is reliable.

**A foreign package with few dependencies.** A self-contained binary from
another distribution usually converts and runs, because the ABI underneath is
the same glibc and the same ELF. `tree` from Rocky installs onto a Debian-built
NETHOS and works.

**A foreign package with a deep dependency tree.** Do not. You will pull in a
second libc under a different name, both will claim `/usr/lib/libc.so.6`, and
npkg will stop you at the conflict — which is the tool working correctly, not a
bug to route around with `--force`.

## The capability index

Names differ between distributions; **sonames do not**. Debian's `libssl3`,
Arch's `openssl` and Fedora's `openssl-libs` all install a file that calls
itself `libssl.so.3`, and every binary linked against it asks for that exact
string. So npkg resolves requirements against what is installed, not against
what somebody called it.

At install time npkg reads the ELF `DT_SONAME` of every library it lays down
and records it in `/var/lib/npkg/capabilities.json`. A requirement is satisfied
by any of, in order:

1. an installed package of that name at an acceptable version
2. a virtual name something declares in `Provides`
3. a **soname** carried by an installed library
4. a **file path** owned by an installed package

Three and four are what make a Fedora package work on a Debian-built system.

```bash
npkg provides libc.so.6        # libc.so.6 is provided by libc6
npkg provides /usr/bin/tree    # provided by tree (file)
npkg check                     # every binary can find its libraries
```

`npkg check` walks the `DT_NEEDED` of every installed ELF and verifies each
soname resolves. A converted package whose real dependencies are missing shows
up there instead of at runtime.

### What this changed for RPM

Fedora's requirements are no longer discarded. `libc.so.6(GLIBC_2.34)(64bit)`
is normalised to `libc.so.6` and kept as a real requirement, because it can now
be checked. Only `rpmlib()` and `config()` are dropped — those describe the
packaging system rather than the machine.

The decorations are stripped rather than honoured: we index sonames, not symbol
versions, and asserting a symbol version we cannot verify would be worse than
not asserting it. A binary needing a genuinely newer glibc will install and
then fail at runtime — the same failure you would get from a manual copy, and
one `npkg check` cannot catch.

### Verified

A `tree` package from **Rocky Linux** — converted from `.rpm`, requiring
`libc.so.6` and `ld-linux-aarch64.so.1` — installed onto a root built entirely
from **Debian** packages. Its requirements resolved against Debian's `libc6`,
and `npkg check` confirmed all 282 binaries across the 9 packages could find
every library they ask for.

That is a genuine cross-distribution install, resolved by content rather than
by name.
