# npkg — the NETHOS package manager

Python, stdlib only, and every on-disk format is JSON you can read in an
editor. It is a library first and a command second:

```python
from npkg import Database, Repository, Transaction
db = Database("/")
Transaction(db, Repository.load_all(db)).install(["tree"])
```

## Commands

```bash
npkg install foo              resolve deps, download, verify, unpack
npkg install ./foo.npk        install a local file, skipping the solver
npkg remove foo               refuses if something still depends on it
npkg list                     what is installed
npkg info foo                 details, installed or from a repo
npkg files foo                what it put on disk
npkg owns /usr/bin/tree       which package a path came from
npkg verify                   check every recorded file still exists
npkg search text              search the repositories
npkg index packages/          generate index.json for a package directory
npkg convert foo.deb          convert Debian and Arch packages
```

Global flags: `--root` (install elsewhere — essential for building images),
`--refresh` (re-fetch indexes), `-n/--dry-run`.

## Getting packages: convert them

You do not have to build a package universe. Arch and Debian packages are both
tarballs with a metadata blob, so `npkg convert` reads their metadata dialect
and writes ours. Nothing is recompiled.

```bash
npkg convert *.deb -o packages/
npkg convert *.pkg.tar.zst -o packages/
npkg index packages/
```

| Source | Format | Metadata |
| --- | --- | --- |
| Arch | tar.zst | `.PKGINFO`, `key = value` |
| Debian | `ar` archive | `control.tar.*` + `data.tar.*`, RFC822 control |

Converted: files and layout, name, version, description, dependencies,
provides/conflicts/replaces, size, and plain-shell maintainer scripts.

**Pick one upstream and stay there.** Dependencies are translated verbatim, so
a converted Debian package asks for `libc6` and a converted Arch package asks
for `glibc` — the same library under two names. Mixing them gives you a solver
asking for packages that do not exist. File layouts differ too (Debian's
multiarch `/usr/lib/x86_64-linux-gnu` against Arch's `/usr/lib`).

Debian epochs (`1:2.3`) are dropped, since npkg has no epoch concept. That can
only make a version look older, never newer, so the worst case is a redundant
upgrade.

## The package format

A `.npk` is a tarball with `.PKGINFO.json` at its root. `tar tf` works on it,
which matters when the tool itself is broken and you are recovering by hand.

```json
{
  "name": "tree", "version": "2.3.2", "release": 1, "arch": "x86_64",
  "summary": "A directory listing program",
  "depends": ["glibc"], "provides": [], "conflicts": [],
  "post_install": "", "pre_remove": ""
}
```

Requirements are `name`, `name>=1.2`, `name==1.2`. Versions compare
segment-wise, so `1.10` is correctly newer than `1.9`.

## The database

`/var/lib/npkg/<package>/` holding `manifest.json` and `files`, one path per
line. Deliberately greppable: if npkg breaks you can still answer "what is
installed" and "what owns this file" with `cat` and `grep`.

## Repositories

`/etc/npkg/repos.json`:

```json
{"repos": [
  {"name": "local", "url": "/var/packages"},
  {"name": "main",  "url": "https://packages.example.com/nethos"}
]}
```

A repository is a directory of `.npk` files plus `index.json` from
`npkg index`. Local paths and HTTP both work. Downloads are checksummed
against the index and a mismatch aborts.

## Safety properties

These are deliberate, and tested:

- **Staged installs.** Files unpack to a temporary directory and are then
  moved into place, so an interrupted install cannot half-install a package.
- **No silent overwrites.** Two packages claiming one path is a hard error.
  Replacing your own file on upgrade is fine; clobbering someone else's is not.
- **Upgrades prune.** Files the old version had and the new one dropped are
  removed, rather than lingering forever.
- **Removal is guarded.** Removing something another package depends on
  requires `--force`.
- **No path escapes.** An archive member resolving outside the root is
  rejected before anything is written.
- **Failed hooks warn, they do not roll back.** By the time a post-install
  script runs the files are already in place; tearing them out would be more
  destructive than leaving them.

## Building an image

`--root` is how you populate a filesystem from outside it:

```bash
npkg --root /mnt/newroot install base linux busybox
```

That is the intended path for building a NETHOS image: point it at a mounted
disk and install into it.
