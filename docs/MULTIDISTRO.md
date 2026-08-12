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

## If you want this to actually work across distributions

The fix is a capability index rather than name matching: record what every
installed file *provides* — sonames, binary paths — and resolve requirements
against that instead of package names. It is how RPM already thinks, and it
would make Fedora's metadata an asset rather than something to discard.

That is a real piece of work, not a flag. It would need:

- a soname index built from ELF `DT_SONAME` at install time
- `Provides:` synthesised from installed file paths
- requirement matching that tries name, then capability, then file

Worth doing if cross-distribution installs matter to you. Say so and it is a
day's work rather than a guess.
