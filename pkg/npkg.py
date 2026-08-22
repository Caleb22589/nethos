#!/usr/bin/env python3
"""
npkg — the NETHOS package manager.

A package manager written to be read. Everything is Python, everything is
stdlib, and every on-disk format is JSON you can open in an editor and
understand without a spec.

    npkg install foo          resolve, download, verify, unpack
    npkg remove foo           remove, refusing if something still needs it
    npkg list / info / files / owns / verify
    npkg build recipes/foo.py build a package from a recipe

It is also a library, which is the point of making it Python:

    from npkg import Database, Repository, Transaction
    db = Database("/")
    Transaction(db, Repository.load_all(db)).install(["busybox"])

Design decisions worth knowing:

  * A package is a tarball with a JSON manifest at its root. Not a custom
    container format — `tar tf` works, and so does recovering a system by
    hand when the tool itself is broken.
  * The database is a directory of JSON files, one per installed package,
    plus a plain text file list. If npkg has a bug you can still answer "what
    is installed" and "what owns this file" with grep.
  * Installs stage into a temporary directory and are then committed, so an
    interrupted install does not leave half a package behind.
  * Files are never silently overwritten across packages. Two packages
    claiming one path is a conflict and stops the transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

__version__ = "0.1.0"

MANIFEST_NAME = ".PKGINFO.json"
DEFAULT_ROOT = os.environ.get("NPKG_ROOT", "/")
DB_DIR = "var/lib/npkg"
CACHE_DIR = "var/cache/npkg"
CONFIG_PATH = "etc/npkg/repos.json"


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------

class NpkgError(Exception):
    """Anything the user should see as a clean message rather than a traceback."""


class DependencyError(NpkgError):
    pass


class ConflictError(NpkgError):
    pass


# ---------------------------------------------------------------------------
# the manifest
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    """What a package says about itself."""

    name: str
    version: str
    release: int = 1
    arch: str = "any"
    summary: str = ""
    description: str = ""
    url: str = ""
    licence: str = ""
    depends: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    replaces: list[str] = field(default_factory=list)
    # Scripts run with the install root as cwd. Kept as plain strings so a
    # manifest stays readable and a package cannot smuggle in a binary hook.
    post_install: str = ""
    pre_remove: str = ""
    # update-alternatives entries lifted out of the Debian postinst at
    # conversion time: [{link, name, path, priority}, ...]
    alternatives: list = field(default_factory=list)
    size: int = 0
    built: str = ""

    @property
    def id(self) -> str:
        return f"{self.name}-{self.version}-{self.release}"

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)


def parse_requirement(text: str) -> tuple[str, str, str]:
    """Split "foo>=1.2" into ("foo", ">=", "1.2"). No operator means any version."""
    for op in (">=", "<=", "==", ">", "<", "="):
        if op in text:
            name, _, version = text.partition(op)
            return name.strip(), ("==" if op == "=" else op), version.strip()
    return text.strip(), "", ""


def version_key(version: str):
    """Compare versions the way a person expects: 1.10 is newer than 1.9.

    Splits into numeric and non-numeric runs so "1.2.3", "1.2.3a" and "1.2.10"
    all order sensibly without pulling in a dependency.
    """
    parts, current, is_digit = [], "", None
    for ch in version:
        if ch in ".-_+~":
            if current:
                parts.append(int(current) if is_digit else current)
            current, is_digit = "", None
            continue
        digit = ch.isdigit()
        if is_digit is None or digit == is_digit:
            current += ch
            is_digit = digit
        else:
            parts.append(int(current) if is_digit else current)
            current, is_digit = ch, digit
    if current:
        parts.append(int(current) if is_digit else current)
    # Numbers sort before strings of the same position; tag each part so the
    # comparison never raises on mixed types.
    return [(0, p, "") if isinstance(p, int) else (1, 0, p) for p in parts]


def satisfies(version: str, op: str, wanted: str) -> bool:
    if not op:
        return True
    a, b = version_key(version), version_key(wanted)
    return {
        "==": a == b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b,
    }[op]


# ---------------------------------------------------------------------------
# package files
# ---------------------------------------------------------------------------

class Package:
    """A .npk on disk: a tarball whose root holds .PKGINFO.json."""

    def __init__(self, path: str):
        self.path = path
        self._manifest: Manifest | None = None

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            with tarfile.open(self.path, "r:*") as tar:
                try:
                    fh = tar.extractfile(MANIFEST_NAME)
                except KeyError:
                    fh = None
                if fh is None:
                    raise NpkgError(f"{self.path}: not an npkg package "
                                    f"(no {MANIFEST_NAME})")
                self._manifest = Manifest.from_dict(json.load(fh))
        return self._manifest

    def files(self) -> list[str]:
        with tarfile.open(self.path, "r:*") as tar:
            return [m.name for m in tar.getmembers()
                    if m.name != MANIFEST_NAME and not m.isdir()]

    def extract(self, root: str) -> list[str]:
        """Unpack into root, returning the installed paths."""
        with tarfile.open(self.path, "r:*") as tar:
            members = [m for m in tar.getmembers() if m.name != MANIFEST_NAME]
            # extract_all validates every path before writing anything.
            extract_all(tar, root, members)
            return [m.name for m in members if not m.isdir()]

    @staticmethod
    def create(manifest: Manifest, source_dir: str, out_path: str,
               compression: str = "gz") -> "Package":
        """Build a .npk from a staging directory."""
        total = 0
        for base, _dirs, names in os.walk(source_dir):
            for n in names:
                # lstat, not getsize: packages legitimately ship symlinks whose
                # target lives in a different package, and following one of
                # those would fail on a link that is not broken at all -- only
                # unresolvable until the rest of the system is installed.
                try:
                    total += os.lstat(os.path.join(base, n)).st_size
                except OSError:
                    pass
        manifest.size = total
        manifest.built = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with tarfile.open(out_path, f"w:{compression}") as tar:
            blob = json.dumps(manifest.to_dict(), indent=2).encode()
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(blob)
            info.mtime = int(time.time())
            import io
            tar.addfile(info, io.BytesIO(blob))
            for entry in sorted(os.listdir(source_dir)):
                tar.add(os.path.join(source_dir, entry), arcname=entry)
        return Package(out_path)


def extract_all(tar: tarfile.TarFile, path: str, members=None) -> list:
    """Extract safely on any Python we might be running on.

    tarfile's `filter` argument arrived in 3.12 (and 3.11.4). Debian bookworm
    ships 3.11.2 — just before it — so passing it unconditionally raises
    TypeError on exactly the machine that builds the images. Paths are checked
    here in either case, so the safety does not depend on the interpreter.
    """
    members = list(members if members is not None else tar.getmembers())
    root = os.path.realpath(path)
    for member in members:
        target = os.path.realpath(os.path.join(path, member.name))
        if target != root and not target.startswith(root + os.sep):
            raise NpkgError(f"unsafe path in archive: {member.name}")
        if member.islnk() or member.issym():
            link = member.linkname
            if os.path.isabs(link):
                continue                    # absolute symlinks resolve at use
            dest = os.path.realpath(os.path.join(
                path, os.path.dirname(member.name), link))
            if dest != root and not dest.startswith(root + os.sep):
                # A link pointing outside the tree is normal in a package whose
                # target lives elsewhere; it only matters if we follow it, and
                # we never do.
                pass
    try:
        tar.extractall(path, members=members, filter="tar")
    except TypeError:
        tar.extractall(path, members=members)
    return members


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# installed database
# ---------------------------------------------------------------------------

class Database:
    """Installed packages: one directory each, holding JSON and a file list."""

    def __init__(self, root: str = DEFAULT_ROOT):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, DB_DIR)
        os.makedirs(self.dir, exist_ok=True)

    def _pkg_dir(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def installed(self) -> dict[str, Manifest]:
        out = {}
        for name in sorted(os.listdir(self.dir)):
            meta = os.path.join(self.dir, name, "manifest.json")
            if os.path.isfile(meta):
                with open(meta) as fh:
                    out[name] = Manifest.from_dict(json.load(fh))
        return out

    def get(self, name: str) -> Manifest | None:
        return self.installed().get(name)

    def is_installed(self, name: str) -> bool:
        return os.path.isfile(os.path.join(self._pkg_dir(name), "manifest.json"))

    def files(self, name: str) -> list[str]:
        path = os.path.join(self._pkg_dir(name), "files")
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]

    def owner(self, path: str) -> str | None:
        """Which package owns a path. Used to catch conflicts before they land.

        An absolute path means "inside the install root" -- `npkg owns
        /usr/bin/tree` asks about the target system, not the host. A relative
        path is resolved against the current directory, so pointing at a
        staged root from outside also works.
        """
        if os.path.isabs(path):
            rel = path.lstrip("/")
        else:
            # realpath on both sides: on macOS /tmp is a symlink to /private/tmp,
            # so comparing unresolved paths silently fails to match.
            rel = os.path.relpath(os.path.realpath(path),
                                  os.path.realpath(self.root))
        for name in self.installed():
            if rel in set(self.files(name)):
                return name
        return None

    def record(self, manifest: Manifest, files: list[str]) -> None:
        d = self._pkg_dir(manifest.name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "manifest.json"), "w") as fh:
            json.dump(manifest.to_dict(), fh, indent=2)
        with open(os.path.join(d, "files"), "w") as fh:
            fh.write("\n".join(files) + "\n")

    def forget(self, name: str) -> None:
        shutil.rmtree(self._pkg_dir(name), ignore_errors=True)

    # -- capabilities ----------------------------------------------------
    # Package names differ between distributions; sonames and file paths do
    # not. The index maps a capability -> the package supplying it, so a
    # requirement can be satisfied by what is actually installed rather than
    # by what somebody called it.

    def _cap_path(self) -> str:
        return os.path.join(self.dir, "capabilities.json")

    def capabilities(self) -> dict[str, str]:
        try:
            with open(self._cap_path()) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def record_capabilities(self, name: str, caps) -> None:
        index = self.capabilities()
        for cap in caps:
            index[cap] = name
        tmp = self._cap_path() + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(index, fh, indent=0, sort_keys=True)
            os.replace(tmp, self._cap_path())
        except OSError:
            pass

    def forget_capabilities(self, name: str) -> None:
        index = self.capabilities()
        remaining = {k: v for k, v in index.items() if v != name}
        if len(remaining) == len(index):
            return
        try:
            with open(self._cap_path(), "w") as fh:
                json.dump(remaining, fh, indent=0, sort_keys=True)
        except OSError:
            pass

    def is_satisfied(self, requirement: str) -> bool:
        """Can this requirement be met by what is installed?

        Four ways, in order of confidence: an installed package of that name at
        an acceptable version, a virtual name something declares it provides, a
        soname carried by an installed library, or a file that exists and is
        owned by a package. The last two are what make a Fedora package's
        `libc.so.6` and `/usr/bin/sh` resolvable on a Debian-built system.
        """
        name, op, version = parse_requirement(requirement)
        installed = self.installed()
        if name in installed and satisfies(installed[name].version, op, version):
            return True
        if name in self.provides_map():
            return True
        if name in self.capabilities():
            return True
        if name.startswith("/"):
            return os.path.lexists(os.path.join(self.root, name.lstrip("/")))
        return False

    def provides_map(self) -> dict[str, str]:
        """Virtual names to the package supplying them."""
        out = {}
        for name, manifest in self.installed().items():
            out[name] = name
            for virtual in manifest.provides:
                out[parse_requirement(virtual)[0]] = name
        return out


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------

# Cloudflare, and anything else in front of a bucket, blocks Python's default
# user agent as a bot. urllib sends "Python-urllib/3.12" and gets 403 while
# curl gets 200 from the same URL, which reads as a permissions problem and is
# not one -- every package download would have failed on the target machine
# with the repository working perfectly.
USER_AGENT = "npkg/1 (+https://github.com/Caleb22589/nethos)"


def _open_url(url: str, timeout: int = 120):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


class Repository:
    """A named source of packages: a URL or a local directory with index.json."""

    def __init__(self, name: str, url: str, root: str = DEFAULT_ROOT):
        self.name = name
        self.url = url.rstrip("/")
        self.root = root
        self.packages: dict[str, list[dict]] = {}

    @property
    def is_local(self) -> bool:
        return not self.url.startswith(("http://", "https://"))

    def index_cache(self) -> str:
        d = os.path.join(self.root, CACHE_DIR, self.name)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "index.json")

    def fetch_index(self, refresh: bool = False) -> None:
        if self.is_local:
            path = os.path.join(self.url, "index.json")
            if not os.path.isfile(path):
                raise NpkgError(f"repo {self.name}: no index at {path}")
            with open(path) as fh:
                data = json.load(fh)
        else:
            cache = self.index_cache()
            if refresh or not os.path.isfile(cache):
                try:
                    with _open_url(self.url + "/index.json",
                                                timeout=30) as resp:
                        blob = resp.read()
                except (urllib.error.URLError, OSError) as exc:
                    if not os.path.isfile(cache):
                        raise NpkgError(f"repo {self.name}: {exc}") from exc
                    blob = None
                if blob is not None:
                    with open(cache, "wb") as fh:
                        fh.write(blob)
            with open(cache) as fh:
                data = json.load(fh)

        self.packages = {}
        for entry in data.get("packages", []):
            self.packages.setdefault(entry["name"], []).append(entry)
        for versions in self.packages.values():
            versions.sort(key=lambda e: version_key(e["version"]), reverse=True)

    def best(self, name: str, op: str = "", wanted: str = "") -> dict | None:
        for entry in self.packages.get(name, []):
            version = entry["version"]
            # A Debian dependency keeps the whole version, revision and all --
            # "gcc-14-base==14.2.0-19" -- while conversion splits that into a
            # version of 14.2.0 and a release of 19. Comparing the halves
            # against the whole never matches, and every exact dependency in
            # the archive failed to resolve from a repository while the
            # package sat in the index.
            release = entry.get("release")
            if "-" in wanted and release not in (None, ""):
                version = f"{version}-{release}"
            if satisfies(version, op, wanted):
                return entry
        return None

    def download(self, entry: dict, refresh: bool = False) -> str:
        filename = entry.get("filename") or f"{entry['name']}-{entry['version']}.npk"
        if self.is_local:
            path = os.path.join(self.url, filename)
            if not os.path.isfile(path):
                raise NpkgError(f"missing package file: {path}")
        else:
            d = os.path.join(self.root, CACHE_DIR, self.name, "packages")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, filename)
            if refresh or not os.path.isfile(path):
                url = f"{self.url}/{filename}"
                tmp = path + ".part"
                try:
                    with _open_url(url, timeout=120) as resp, \
                            open(tmp, "wb") as fh:
                        shutil.copyfileobj(resp, fh)
                except (urllib.error.URLError, OSError) as exc:
                    raise NpkgError(f"download failed: {url}: {exc}") from exc
                os.replace(tmp, path)

        expected = entry.get("sha256")
        if expected:
            actual = sha256_file(path)
            if actual != expected:
                raise NpkgError(
                    f"checksum mismatch for {entry['name']}\n"
                    f"  expected {expected}\n  got      {actual}")
        return path

    @staticmethod
    def load_all(db: Database, refresh: bool = False) -> list["Repository"]:
        config = os.path.join(db.root, CONFIG_PATH)
        if not os.path.isfile(config):
            return []
        with open(config) as fh:
            data = json.load(fh)
        repos = []
        for entry in data.get("repos", []):
            repo = Repository(entry["name"], entry["url"], db.root)
            repo.fetch_index(refresh=refresh)
            repos.append(repo)
        return repos


# ---------------------------------------------------------------------------
# solving
# ---------------------------------------------------------------------------

class Solver:
    """Works out what to install, and in what order."""

    def __init__(self, db: Database, repos: list[Repository]):
        self.db = db
        self.repos = repos

    def find(self, name: str, op: str = "", version: str = "") -> tuple[Repository, dict]:
        for repo in self.repos:
            entry = repo.best(name, op, version)
            if entry:
                return repo, entry

        # Nothing is called that. Something may still provide it.
        #
        # base-files depends on "awk", and no package is named awk: gawk
        # declares it in Provides. The installed-package side of this was
        # already handled by Database.provides_map, so a virtual dependency
        # resolved fine once something supplying it was installed and failed
        # outright when it had to come from a repository -- which is every
        # dependency during an install.
        #
        # Unversioned only. A versioned dependency on a virtual name is not
        # something Debian expresses, and guessing at what version a provider
        # would satisfy is worse than saying it is not there.
        if not op:
            for repo in self.repos:
                for candidates in repo.packages.values():
                    for entry in candidates:
                        if name in (entry.get("provides") or []):
                            return repo, entry

        want = f"{name}{op}{version}" if op else name
        if not self.repos:
            # The common case on a fresh system: nothing is configured, so
            # `install` has nowhere to look. Say that, rather than implying the
            # package does not exist.
            raise DependencyError(
                f"no repositories are configured, so there is nowhere to find "
                f"'{want}'.\n"
                f"  To install from Debian's archive:  npkg fetch {name}\n"
                f"  To install a local file:           npkg install ./{name}.npk\n"
                f"  To configure a repository:         edit /etc/npkg/repos.json")
        raise DependencyError(f"no package satisfies '{want}'")

    def resolve(self, names: list[str], reinstall: bool = False
                ) -> list[tuple[Repository, dict]]:
        """Depth-first over dependencies, returning install order.

        Cycles are tolerated rather than fatal: circular dependencies are
        common in real package sets and only matter if the scripts care.
        """
        chosen: dict[str, tuple[Repository, dict]] = {}
        order: list[str] = []
        visiting: set[str] = set()
        installed = self.db.provides_map()

        def visit(requirement: str, chain: list[str]):
            name, op, version = parse_requirement(requirement)
            if name in chosen or name in visiting:
                return
            if name in installed and not (reinstall and name in names):
                current = self.db.get(installed[name])
                if current and satisfies(current.version, op, version):
                    return
            # Not a package we know by name -- but a soname or a file path may
            # already satisfy it, which is how converted RPM requirements get
            # resolved against a Debian-built system.
            if requirement not in names and self.db.is_satisfied(requirement):
                return
            visiting.add(name)
            try:
                repo, entry = self.find(name, op, version)
            except DependencyError as exc:
                if chain:
                    raise DependencyError(
                        f"{exc} (needed by {' -> '.join(chain)})") from exc
                raise
            for dep in entry.get("depends", []):
                visit(dep, chain + [name])
            visiting.discard(name)
            chosen[name] = (repo, entry)
            order.append(name)

        for requested in names:
            visit(requested, [])
        return [chosen[n] for n in order]

    def dependents(self, name: str) -> list[str]:
        """Installed packages that would break if `name` went away."""
        out = []
        for other, manifest in self.db.installed().items():
            if other == name:
                continue
            for dep in manifest.depends:
                if parse_requirement(dep)[0] == name:
                    out.append(other)
                    break
        return out


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------

class Transaction:
    def __init__(self, db: Database, repos: list[Repository], *,
                 verbose: bool = True, dry_run: bool = False):
        self.db = db
        self.repos = repos
        self.solver = Solver(db, repos)
        self.verbose = verbose
        self.dry_run = dry_run
        # path -> owning package, built once and maintained as we go. Without
        # it, checking each new package against every installed one re-reads
        # every file list from disk: installing 150 packages, some shipping
        # thousands of kernel modules, turns into millions of redundant reads
        # and looks exactly like a hang.
        self._owners: dict[str, str] | None = None

    def _owner_map(self) -> dict[str, str]:
        if self._owners is None:
            self._owners = {}
            for name in self.db.installed():
                for rel in self.db.files(name):
                    self._owners[rel] = name
        return self._owners

    def say(self, text: str) -> None:
        if self.verbose:
            print(text)

    # -- install ---------------------------------------------------------
    def install(self, names: list[str], reinstall: bool = False) -> list[str]:
        plan = self.solver.resolve(names, reinstall=reinstall)
        if not plan:
            self.say("nothing to do — already installed")
            return []

        self.say("Installing: " + ", ".join(
            f"{e['name']}-{e['version']}" for _r, e in plan))
        if self.dry_run:
            return [e["name"] for _r, e in plan]

        done = []
        for repo, entry in plan:
            path = repo.download(entry)
            done.append(self._install_file(path, entry.get("name")))
        return done

    def install_files(self, paths: list[str]) -> list[str]:
        return [self._install_file(p) for p in paths]

    def _install_file(self, path: str, expect_name: str | None = None) -> str:
        pkg = Package(path)
        manifest = pkg.manifest
        if expect_name and manifest.name != expect_name:
            raise NpkgError(
                f"{path} claims to be {manifest.name}, index said {expect_name}")

        # Refuse to trample another package's files. Replacing our own copy on
        # upgrade is fine; silently clobbering someone else's is not.
        incoming = set(pkg.files())
        owners = self._owner_map()
        clashes: dict[str, list[str]] = {}
        for rel in incoming:
            other = owners.get(rel)
            if other and other != manifest.name:
                clashes.setdefault(other, []).append(rel)
        if clashes:
            other, paths = next(iter(clashes.items()))
            raise ConflictError(
                f"{manifest.name} and {other} both provide: "
                f"{', '.join(sorted(paths)[:3])}")

        for conflict in manifest.conflicts:
            # Honour the version constraint. Debian's conflicts are nearly
            # always bounded -- udev declares "Conflicts: systemd (<< 255)",
            # meaning it conflicts with *old* systemd, not with systemd. Reading
            # only the name refuses packages that are perfectly compatible, and
            # the damage is silent: refusing udev cost udevadm, systemd-udevd
            # and every udev rule, so the initramfs had nothing to populate
            # /dev/disk/by-uuid with and root=UUID= could never resolve.
            cname, op, cversion = parse_requirement(conflict)
            other = self.db.get(cname)
            if other and satisfies(other.version, op, cversion):
                detail = f" ({cname} {other.version} matches {op}{cversion})" \
                    if op else ""
                raise ConflictError(
                    f"{manifest.name} conflicts with {cname}{detail}")

        previous = self.db.get(manifest.name)
        old_files = set(self.db.files(manifest.name)) if previous else set()

        # Stage first, commit second: an interrupted unpack should not leave a
        # half-installed package behind.
        staging = tempfile.mkdtemp(prefix="npkg-stage-", dir=os.path.join(
            self.db.root, CACHE_DIR) if os.path.isdir(
                os.path.join(self.db.root, CACHE_DIR)) else None)
        try:
            pkg.extract(staging)
            files, directories = [], []
            for base, dirnames, names in os.walk(staging):
                for d in dirnames:
                    directories.append(os.path.relpath(os.path.join(base, d), staging))
                for n in names:
                    full = os.path.join(base, n)
                    files.append(os.path.relpath(full, staging))

            # Create directories first, including the empty ones. A package
            # shipping an empty directory means it: systemd ships
            # /etc/systemd/system empty and expects to find it later. Walking
            # only for files silently drops those, and the breakage surfaces
            # much later as something unrelated failing to write there.
            for rel in sorted(directories):
                target = os.path.join(self.db.root, rel)
                if os.path.isdir(target):
                    continue                  # already there, or a symlink to it
                os.makedirs(target, exist_ok=True)
                try:
                    shutil.copystat(os.path.join(staging, rel), target)
                except OSError:
                    pass

            for rel in files:
                src = os.path.join(staging, rel)
                dst = os.path.join(self.db.root, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                # Replacing an existing path (an upgrade, or a symlink another
                # package already placed) needs it gone first: os.replace will
                # not overwrite a symlink with a file.
                if os.path.islink(dst) or os.path.exists(dst):
                    try:
                        os.remove(dst)
                    except IsADirectoryError:
                        shutil.rmtree(dst, ignore_errors=True)
                shutil.move(src, dst)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        # An upgrade leaves behind files the new version dropped.
        for stale in old_files - set(files):
            target = os.path.join(self.db.root, stale)
            if os.path.isfile(target):
                os.remove(target)

        self.db.record(manifest, files)
        self._index_capabilities(manifest, files)
        if self._owners is not None:
            for rel in old_files - set(files):
                self._owners.pop(rel, None)
            for rel in files:
                self._owners[rel] = manifest.name
        verb = "upgraded" if previous else "installed"
        self.say(f"  {verb} {manifest.id} ({len(files)} files)")

        if manifest.alternatives:
            self._apply_alternatives(manifest)

        if manifest.post_install:
            self._run_script(manifest.post_install, manifest.name, "post-install")
        return manifest.name

    def _index_capabilities(self, manifest: Manifest, files: list[str]) -> None:
        """Record what this package provides beyond its own name."""
        caps = set(manifest.provides)
        try:
            from npkg_elf import scan             # noqa: PLC0415
            provides, _requires = scan(self.db.root, files)
            caps.update(provides)
        except ImportError:
            pass
        if caps:
            self.db.record_capabilities(manifest.name, caps)

    def _apply_alternatives(self, manifest: Manifest) -> None:
        """Create the symlinks update-alternatives would have made.

        Debian packages ship /usr/bin/vim.basic and create /usr/bin/vim in the
        postinst. We do not run postinsts, so without this the package installs
        perfectly and the command does not exist -- which is exactly how it
        looks from the outside: "installed 8 files", then "vim: command not
        found".

        The two-level layout is Debian's: link -> /etc/alternatives/name ->
        the real binary, so switching an implementation is one symlink.
        """
        state_path = os.path.join(self.db.dir, "alternatives.json")
        try:
            with open(state_path) as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}

        alt_dir = os.path.join(self.db.root, "etc", "alternatives")
        os.makedirs(alt_dir, exist_ok=True)

        for entry in manifest.alternatives:
            name = entry.get("name")
            link = entry.get("link", "")
            target = entry.get("path", "")
            priority = int(entry.get("priority", 0))
            if not (name and link and target):
                continue
            if not os.path.exists(os.path.join(self.db.root, target.lstrip("/"))):
                continue                      # the binary is in another package
            # Highest priority wins, as update-alternatives does in auto mode.
            if name in state and state[name]["priority"] > priority:
                continue

            alt = os.path.join(alt_dir, name)
            link_path = os.path.join(self.db.root, link.lstrip("/"))
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            for path, dest in ((alt, target), (link_path, "/etc/alternatives/" + name)):
                if os.path.islink(path) or os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        continue
                os.symlink(dest, path)
            state[name] = {"priority": priority, "path": target,
                           "link": link, "package": manifest.name}

        try:
            with open(state_path, "w") as fh:
                json.dump(state, fh, indent=2)
        except OSError:
            pass

    # -- remove ----------------------------------------------------------
    def remove(self, names: list[str], force: bool = False) -> list[str]:
        for name in names:
            if not self.db.is_installed(name):
                raise NpkgError(f"not installed: {name}")
            needed_by = [d for d in self.solver.dependents(name) if d not in names]
            if needed_by and not force:
                raise DependencyError(
                    f"{name} is required by: {', '.join(needed_by)}\n"
                    f"  use --force to remove it anyway")

        if self.dry_run:
            self.say("Would remove: " + ", ".join(names))
            return names

        for name in names:
            manifest = self.db.get(name)
            if manifest and manifest.pre_remove:
                self._run_script(manifest.pre_remove, name, "pre-remove")

            files = self.db.files(name)
            directories = set()
            for rel in files:
                target = os.path.join(self.db.root, rel)
                if os.path.isfile(target) or os.path.islink(target):
                    os.remove(target)
                directories.add(os.path.dirname(target))
            # Tidy up directories the package left empty, deepest first.
            for d in sorted(directories, key=len, reverse=True):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
            self.db.forget(name)
            self.db.forget_capabilities(name)
            if self._owners is not None:
                for rel in files:
                    self._owners.pop(rel, None)
            self.say(f"  removed {name} ({len(files)} files)")
        return names

    def _run_script(self, script: str, name: str, phase: str,
                    timeout: int = 60) -> None:
        import subprocess
        self.say(f"  running {phase} script for {name}")
        env = dict(os.environ,
                   DEBIAN_FRONTEND="noninteractive",
                   DEBCONF_NONINTERACTIVE_SEEN="true",
                   PATH="/usr/bin:/usr/local/bin")
        try:
            result = subprocess.run(
                ["/bin/sh", "-c", script], cwd=self.db.root,
                capture_output=True, text=True, env=env,
                # A hook must never be able to wait on input: debconf's postinst
                # blocks on stdin forever, which stalls the whole install with
                # no error and no output. And a hook that runs away is capped
                # rather than allowed to hang the transaction.
                stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"warning: {phase} script for {name} timed out after "
                  f"{timeout}s and was killed", file=sys.stderr)
            return
        if result.returncode != 0:
            # A failed hook is a warning, not a rollback: the files are already
            # in place and removing them would be more destructive than useful.
            print(f"warning: {phase} script for {name} exited "
                  f"{result.returncode}: {result.stderr.strip()}", file=sys.stderr)

    # -- verify ----------------------------------------------------------
    def verify(self, names: list[str] | None = None) -> dict[str, list[str]]:
        problems = {}
        for name in (names or list(self.db.installed())):
            # lexists, not exists: a symlink pointing into a package you have
            # not installed is still present and correct. Following it would
            # report a perfectly good file as missing.
            missing = [rel for rel in self.db.files(name)
                       if not os.path.lexists(os.path.join(self.db.root, rel))]
            if missing:
                problems[name] = missing
        return problems


# ---------------------------------------------------------------------------
# repository index generation
# ---------------------------------------------------------------------------

def build_index(directory: str) -> dict:
    """Scan a directory of .npk files into an index.json."""
    packages = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".npk"):
            continue
        path = os.path.join(directory, name)
        manifest = Package(path).manifest
        entry = manifest.to_dict()
        entry["filename"] = name
        entry["sha256"] = sha256_file(path)
        entry["size"] = os.path.getsize(path)
        packages.append(entry)
    index = {"version": 1,
             "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "packages": packages}
    with open(os.path.join(directory, "index.json"), "w") as fh:
        json.dump(index, fh, indent=2)
    return index


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_install(args, db, repos):
    tx = Transaction(db, repos, dry_run=args.dry_run)
    local = [n for n in args.names if n.endswith(".npk") and os.path.isfile(n)]
    remote = [n for n in args.names if n not in local]
    if local:
        tx.install_files(local)
    if remote:
        tx.install(remote, reinstall=args.reinstall)


def cmd_upgrade(args, db, repos):
    """Install the newer version of anything the repository has moved on.

    npkg had no way to do this at all: packages could be installed and
    removed, and a system could only get newer by being reinstalled. An
    operating system that cannot take a security fix without a reinstall is
    not finished.

    Deliberately simple. It compares what is installed against what the
    repository offers and installs the difference; it does not try to be
    clever about held versions or partial upgrades, because the failure mode
    of clever here is a machine that will not boot.
    """
    if not repos:
        raise NpkgError(
            "no repositories are configured, so there is nothing to upgrade "
            "from.\n  Configure one in /etc/npkg/repos.json")

    installed = db.installed()
    if not installed:
        print("no packages installed")
        return

    plan = []
    for name, manifest in sorted(installed.items()):
        best, best_repo = None, None
        for repo in repos:
            entry = repo.best(name)
            if entry and (best is None or
                          version_key(entry["version"]) > version_key(best["version"])):
                best, best_repo = entry, repo
        if best and version_key(best["version"]) > version_key(manifest.version):
            plan.append((name, manifest.version, best["version"], best_repo, best))

    if not plan:
        print(f"up to date — {len(installed)} packages, none newer in the repository")
        return

    print(f"{len(plan)} package(s) to upgrade:")
    for name, old, new, _r, _e in plan:
        print(f"  {name:<28} {old}  ->  {new}")
    if args.dry_run:
        return
    if not args.yes:
        try:
            if input("\ncontinue? [y/N] ").strip().lower() not in ("y", "yes"):
                print("nothing done")
                return
        except EOFError:
            print("nothing done")
            return

    tx = Transaction(db, repos)
    # One at a time, so a package that fails leaves the rest of the system
    # upgraded rather than the whole run rolled back into an unknown state.
    failed = []
    for name, _old, _new, _repo, _entry in plan:
        try:
            tx.install([name], reinstall=True)
        except NpkgError as exc:
            failed.append((name, exc))
            print(f"  {name}: {exc}")
    if failed:
        print(f"\n{len(failed)} of {len(plan)} failed; the rest are upgraded")
    else:
        print(f"\nupgraded {len(plan)} package(s)")


def cmd_remove(args, db, repos):
    Transaction(db, repos, dry_run=args.dry_run).remove(args.names, force=args.force)


def cmd_list(args, db, repos):
    installed = db.installed()
    if not installed:
        print("no packages installed")
        return
    for name, manifest in installed.items():
        print(f"{name:<24} {manifest.version:<12} {manifest.summary}")


def cmd_info(args, db, repos):
    manifest = db.get(args.name)
    source = "installed"
    if manifest is None:
        for repo in repos:
            entry = repo.best(args.name)
            if entry:
                manifest, source = Manifest.from_dict(entry), f"repo {repo.name}"
                break
    if manifest is None:
        raise NpkgError(f"unknown package: {args.name}")
    print(f"{manifest.name} {manifest.version}-{manifest.release}  [{source}]")
    for label, value in (("Summary", manifest.summary), ("URL", manifest.url),
                         ("Licence", manifest.licence),
                         ("Depends", ", ".join(manifest.depends)),
                         ("Provides", ", ".join(manifest.provides)),
                         ("Size", f"{manifest.size/1024:.1f} KB" if manifest.size else "")):
        if value:
            print(f"  {label:<9} {value}")
    if manifest.description:
        print(f"\n{manifest.description}")


def cmd_files(args, db, repos):
    if not db.is_installed(args.name):
        raise NpkgError(f"not installed: {args.name}")
    for rel in db.files(args.name):
        print("/" + rel)


def cmd_owns(args, db, repos):
    owner = db.owner(args.path)
    print(f"{args.path} is owned by {owner}" if owner
          else f"{args.path} is not owned by any package")


def cmd_verify(args, db, repos):
    problems = Transaction(db, repos).verify(args.names or None)
    if not problems:
        print("all packages verify")
        return
    for name, missing in problems.items():
        print(f"{name}: {len(missing)} missing files")
        for rel in missing[:10]:
            print(f"  /{rel}")
    raise SystemExit(1)


def cmd_search(args, db, repos):
    q = args.query.lower()
    seen = set()

    if not repos:
        # Nothing configured locally: search what `npkg fetch` would install
        # from, so search is useful on a fresh system rather than always empty.
        import platform                                # noqa: PLC0415
        from npkg_bootstrap import DebianArchive       # noqa: PLC0415
        arch = DEB_ARCH.get(platform.machine(), "arm64")
        work = os.path.join(db.root, CACHE_DIR, "debian")
        os.makedirs(work, exist_ok=True)
        archive = DebianArchive(arch=arch, cache=work)
        archive.load()
        hits = 0
        for name, fields in sorted(archive.packages.items()):
            summary = fields.get("Description", "").split("\n")[0]
            if q in name.lower() or q in summary.lower():
                mark = "*" if db.is_installed(name) else " "
                print(f"{mark} {name:<28} {fields.get('Version',''):<22} {summary[:60]}")
                hits += 1
                if hits >= 40:
                    print("  … more matches; narrow the search")
                    break
        if not hits:
            print("nothing found")
        else:
            print(f"\ninstall with:  npkg fetch <name>")
        return
    for repo in repos:
        for name, entries in repo.packages.items():
            entry = entries[0]
            haystack = f"{name} {entry.get('summary','')}".lower()
            if q in haystack and name not in seen:
                seen.add(name)
                mark = "*" if db.is_installed(name) else " "
                print(f"{mark} {name:<22} {entry['version']:<12} "
                      f"{entry.get('summary','')}")
    if not seen:
        print("nothing found")


def cmd_index(args, db, repos):
    index = build_index(args.directory)
    print(f"indexed {len(index['packages'])} packages in {args.directory}")


DEB_ARCH = {"aarch64": "arm64", "arm64": "arm64",
            "x86_64": "amd64", "amd64": "amd64",
            "armv7l": "armhf", "riscv64": "riscv64"}


def default_suite() -> str:
    """The Debian suite this system was actually built from.

    Recorded at bootstrap in /etc/npkg/suite. The default here used to be
    "bookworm" while the image was built from trixie, so `npkg fetch` pulled
    packages from a different release than the one on disk -- installing
    bookworm's libglib2.0-bin alongside trixie's glib, which is exactly the
    cross-release mixing the conflict checks exist to prevent, arriving through
    the front door.
    """
    try:
        with open("/etc/npkg/suite") as fh:
            suite = fh.read().strip()
            if suite:
                return suite
    except OSError:
        pass
    return "trixie"


def cmd_fetch(args, db, repos):
    """Install straight from Debian's archive: resolve, convert, install.

    This is what makes npkg usable on the running system rather than only at
    image-build time. Debian's index is the dependency authority; the packages
    become npkg packages on the way in, and nothing Debian-specific is left
    behind on disk.
    """
    import platform                                    # noqa: PLC0415
    from npkg_bootstrap import DebianArchive           # noqa: PLC0415
    from npkg_convert import convert_deb               # noqa: PLC0415

    arch = args.arch or DEB_ARCH.get(platform.machine(), "arm64")
    work = os.path.join(db.root, CACHE_DIR, "debian")
    os.makedirs(work, exist_ok=True)

    print(f"Debian {args.suite}/{arch}")
    archive = DebianArchive(args.mirror, args.suite, arch, cache=work)
    archive.load()

    resolved = archive.resolve(args.names, with_recommends=args.recommends)
    installed = db.installed()
    todo = [f for f in resolved if f["Package"] not in installed]
    if not todo:
        print("nothing to do — already installed")
        return

    size = sum(int(f.get("Size", 0)) for f in todo) / 1e6
    print(f"{len(todo)} packages to install ({size:.1f} MB)")
    for f in todo:
        print(f"  {f['Package']:<28} {f.get('Version','')}")
    if args.dry_run:
        return
    if not args.yes:
        try:
            if input("continue? [Y/n] ").strip().lower() not in ("", "y", "yes"):
                print("cancelled")
                return
        except EOFError:
            pass

    debs, npks = os.path.join(work, "debs"), os.path.join(work, "packages")
    tx = Transaction(db, [])
    for i, fields in enumerate(todo, 1):
        name = fields["Package"]
        print(f"[{i}/{len(todo)}] {name}")
        deb = archive.download(fields, debs)
        npk = convert_deb(deb, npks, layout=args.layout)
        try:
            tx.install_files([npk])
        except NpkgError as exc:
            print(f"  skipped: {exc}")
    print("done")


def cmd_provides(args, db, repos):
    """Who supplies a capability -- a soname, a virtual name, or a file."""
    what = args.capability
    caps = db.capabilities()
    if what in caps:
        print(f"{what} is provided by {caps[what]}")
        return
    virtual = db.provides_map()
    if what in virtual:
        print(f"{what} is provided by {virtual[what]} (declared)")
        return
    if what.startswith("/"):
        owner = db.owner(what)
        if owner:
            print(f"{what} is provided by {owner} (file)")
            return
    print(f"nothing provides {what}")
    raise SystemExit(1)


def cmd_check(args, db, repos):
    """Verify every installed binary can find the libraries it asks for.

    This is the capability index earning its keep: it walks the DT_NEEDED of
    every ELF on the system and checks each soname against what is actually
    installed. A package converted from another distribution whose real
    dependencies are missing shows up here rather than at runtime.
    """
    from npkg_elf import needed                   # noqa: PLC0415

    caps = db.capabilities()
    installed = db.installed()
    missing: dict[str, set] = {}
    checked = 0

    for name in sorted(installed):
        for rel in db.files(name):
            full = os.path.join(db.root, rel)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            wants = needed(full)
            if not wants:
                continue
            checked += 1
            for soname in wants:
                if soname in caps:
                    continue
                # The dynamic loader itself is not shipped under its soname.
                if soname.startswith("ld-") or soname.startswith("ld64"):
                    continue
                missing.setdefault(name, set()).add(soname)

    print(f"checked {checked} binaries in {len(installed)} packages")
    if not missing:
        print("every library dependency is satisfied")
        return
    print(f"\n{len(missing)} packages have unsatisfied libraries:")
    for name, sonames in sorted(missing.items()):
        print(f"  {name}: {', '.join(sorted(sonames))}")
    raise SystemExit(1)


def cmd_service(args, db, repos):
    from npkg_service import main as service_main    # noqa: PLC0415
    service_main(args, db)


def cmd_convert(args, db, repos):
    from npkg_convert import convert             # noqa: PLC0415
    for path in args.packages:
        out = convert(path, args.output, args.layout,
                      getattr(args, "scripts", False))
        print(f"{os.path.basename(path)}  ->  {os.path.basename(out)}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="npkg", description="the NETHOS package manager")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="install root (default /, or $NPKG_ROOT)")
    parser.add_argument("--refresh", action="store_true",
                        help="re-fetch repository indexes")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("--version", action="version", version=f"npkg {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="install packages")
    p.add_argument("names", nargs="+")
    p.add_argument("--reinstall", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("upgrade", help="upgrade everything the repository has newer")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would change and stop")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask")
    p.set_defaults(func=cmd_upgrade)

    p = sub.add_parser("remove", help="remove packages")
    p.add_argument("names", nargs="+")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="list installed packages")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="show package details")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("files", help="list a package's files")
    p.add_argument("name")
    p.set_defaults(func=cmd_files)

    p = sub.add_parser("owns", help="which package owns a path")
    p.add_argument("path")
    p.set_defaults(func=cmd_owns)

    p = sub.add_parser("verify", help="check installed files are present")
    p.add_argument("names", nargs="*")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("search", help="search the repositories")
    p.add_argument("query")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("index", help="generate index.json for a package directory")
    p.add_argument("directory")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("fetch",
                       help="install packages straight from Debian's archive")
    p.add_argument("names", nargs="+")
    p.add_argument("--suite", default=default_suite())
    p.add_argument("--mirror", default="http://deb.debian.org/debian")
    p.add_argument("--arch", default="", help="Debian arch (default: this machine)")
    p.add_argument("--layout", choices=("native", "arch"), default="arch")
    p.add_argument("--recommends", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("provides", help="what supplies a soname, name or file")
    p.add_argument("capability")
    p.set_defaults(func=cmd_provides)

    p = sub.add_parser("check",
                       help="verify every binary can find its libraries")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("service", help="enable/disable systemd units")
    p.add_argument("action", choices=("list", "enable", "disable"))
    p.add_argument("names", nargs="*")
    p.add_argument("--user", action="store_true", help="the per-user manager")
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("convert",
                       help="convert Arch (.pkg.tar.*) or Debian (.deb) packages")
    p.add_argument("packages", nargs="+")
    p.add_argument("-o", "--output", default="./packages")
    p.add_argument("--layout", choices=("native", "arch"), default="arch")
    p.add_argument("--scripts", action="store_true",
                   help="also carry maintainer scripts (they usually assume dpkg)")
    p.set_defaults(func=cmd_convert)

    args = parser.parse_args(argv)

    # Only touch the install root for commands that actually read or write it.
    # `index` and `build` operate on a directory of files and have no business
    # creating /var/lib/npkg — least of all on a machine that is not the target.
    needs_db = args.command not in ("index", "build", "convert")
    # upgrade was added to the parser and not to this list, so it loaded no
    # repositories and then complained that none were configured -- with a
    # correct repos.json sitting right there.
    needs_repos = args.command in ("install", "upgrade", "search", "info", "remove")
    try:
        db = Database(args.root) if needs_db else None
        repos = Repository.load_all(db, refresh=args.refresh) if needs_repos else []
        args.func(args, db, repos)
    except NpkgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
