#!/usr/bin/env python3
"""
npkg_service — enable and disable systemd units without systemctl.

Debian packages do not enable their own services by shipping symlinks; a
postinst calls `deb-systemd-helper enable`. We do not run postinsts, so an
installed service is present and dead: no symlink, no autostart, and no error
to tell you why.

`systemctl enable` is not magic either. It reads the unit's [Install] section
and creates symlinks:

    WantedBy=multi-user.target
        -> /etc/systemd/system/multi-user.target.wants/foo.service

so this does the same thing by reading the unit file. That means it works on a
root that is not booted -- which is exactly what image building needs, and
where systemctl refuses to help.

    npkg service list                what is installed, and whether it is on
    npkg service enable  foo         make the [Install] symlinks
    npkg service disable foo         remove them
    npkg service enable --user foo   the per-user manager instead
"""

from __future__ import annotations

import os
import re

UNIT_DIRS = ("usr/lib/systemd/system", "lib/systemd/system", "etc/systemd/system")
USER_UNIT_DIRS = ("usr/lib/systemd/user", "lib/systemd/user", "etc/systemd/user")


def unit_dirs(root: str, user: bool = False) -> list[str]:
    dirs = USER_UNIT_DIRS if user else UNIT_DIRS
    return [os.path.join(root, d) for d in dirs]


def find_unit(root: str, name: str, user: bool = False) -> str | None:
    """Locate a unit file. A bare name gets .service appended, as systemd does."""
    if "." not in name:
        name += ".service"
    for directory in unit_dirs(root, user):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def parse_install(path: str) -> dict[str, list[str]]:
    """Read the [Install] section: WantedBy, RequiredBy, Also, Alias."""
    out: dict[str, list[str]] = {"WantedBy": [], "RequiredBy": [],
                                 "Also": [], "Alias": []}
    section = ""
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1]
                    continue
                if section != "Install" or "=" not in line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in out:
                    # systemd allows several targets on one line, space separated.
                    out[key].extend(v for v in value.split() if v)
    except OSError:
        pass
    return out


def list_units(root: str, user: bool = False) -> list[dict]:
    seen: dict[str, dict] = {}
    for directory in unit_dirs(root, user):
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if not name.endswith((".service", ".socket", ".timer", ".target",
                                  ".mount", ".path")):
                continue
            if name in seen:
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            install = parse_install(path)
            seen[name] = {
                "name": name,
                "path": path,
                "enabled": is_enabled(root, name, user),
                "wanted_by": install["WantedBy"] + install["RequiredBy"],
            }
    return list(seen.values())


def _link_targets(root: str, name: str, install: dict, user: bool) -> list[tuple[str, str]]:
    """The (symlink, target) pairs `systemctl enable` would create."""
    base = os.path.join(root, "etc/systemd", "user" if user else "system")
    pairs = []
    for target in install["WantedBy"]:
        pairs.append((os.path.join(base, target + ".wants", name), None))
    for target in install["RequiredBy"]:
        pairs.append((os.path.join(base, target + ".requires", name), None))
    for alias in install["Alias"]:
        pairs.append((os.path.join(base, alias), None))
    return pairs


def is_enabled(root: str, name: str, user: bool = False) -> bool:
    if "." not in name:
        name += ".service"
    base = os.path.join(root, "etc/systemd", "user" if user else "system")
    if not os.path.isdir(base):
        return False
    for entry in os.listdir(base):
        if entry.endswith((".wants", ".requires")):
            if os.path.islink(os.path.join(base, entry, name)):
                return True
    return False


def enable(root: str, name: str, user: bool = False, seen: set | None = None) -> list[str]:
    """Create the [Install] symlinks. Returns what was linked."""
    if "." not in name:
        name += ".service"
    seen = seen if seen is not None else set()
    if name in seen:
        return []
    seen.add(name)

    path = find_unit(root, name, user)
    if path is None:
        raise FileNotFoundError(f"no unit named {name}")

    install = parse_install(path)
    if not any(install.values()):
        # A unit with no [Install] cannot be enabled -- it is pulled in by
        # something else, or started by hand. systemctl says the same.
        return []

    # The symlink points at the unit's real location, the way systemctl does,
    # so `systemctl status` resolves it to the shipped file.
    unit_ref = "/" + os.path.relpath(path, root)

    made = []
    for link, _ in _link_targets(root, name, install, user):
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.islink(link) or os.path.exists(link):
            try:
                os.remove(link)
            except OSError:
                continue
        os.symlink(unit_ref, link)
        made.append("/" + os.path.relpath(link, root))

    # `Also=` pulls in companion units, most often a .socket beside a .service.
    for also in install["Also"]:
        try:
            made += enable(root, also, user, seen)
        except FileNotFoundError:
            pass
    return made


def disable(root: str, name: str, user: bool = False) -> list[str]:
    if "." not in name:
        name += ".service"
    base = os.path.join(root, "etc/systemd", "user" if user else "system")
    removed = []
    if not os.path.isdir(base):
        return removed
    for entry in os.listdir(base):
        if not entry.endswith((".wants", ".requires")):
            continue
        link = os.path.join(base, entry, name)
        if os.path.islink(link):
            os.remove(link)
            removed.append("/" + os.path.relpath(link, root))
    return removed


# ---------------------------------------------------------------------------
# CLI, called from npkg's `service` subcommand
# ---------------------------------------------------------------------------

def main(args, db):
    root = db.root
    if args.action == "list":
        units = list_units(root, args.user)
        if not units:
            print("no units installed")
            return
        width = max(len(u["name"]) for u in units)
        for unit in sorted(units, key=lambda u: (not u["enabled"], u["name"])):
            state = "enabled " if unit["enabled"] else "disabled"
            if not unit["wanted_by"]:
                state = "static  "
            print(f"{state}  {unit['name']:<{width}}  "
                  f"{' '.join(unit['wanted_by'])}")
        return

    if not args.names:
        raise SystemExit(f"npkg service {args.action}: needs a unit name")

    for name in args.names:
        try:
            if args.action == "enable":
                made = enable(root, name, args.user)
                if made:
                    for link in made:
                        print(f"  created {link}")
                else:
                    print(f"  {name}: no [Install] section, nothing to enable")
            else:
                removed = disable(root, name, args.user)
                for link in removed:
                    print(f"  removed {link}")
                if not removed:
                    print(f"  {name}: was not enabled")
        except FileNotFoundError as exc:
            print(f"  error: {exc}")
