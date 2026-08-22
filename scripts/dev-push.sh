#!/bin/sh
# Put a payload change on the NETHOS stick, from macOS, in seconds.
#
#   scripts/dev-push.sh                    copy payload/ to the stick's ESP
#   scripts/dev-push.sh --kernel FILE.tar.gz  ...and a freshly built kernel
#   scripts/dev-push.sh --clear            remove it; the stick boots stock again
#
# The ESP is FAT and macOS mounts it without help, so this needs no Linux host,
# no loop mount, no image build and no reflash. Plug the stick in, run this,
# put it back in the machine: nethos-devsync applies it at boot.
#
# This exists because a one-line change to the shell used to cost about fifty
# minutes -- a cross-compiled kernel and an emulated image build -- purely
# because nothing could carry a file to the machine. Only kernel changes need
# a real build now.
set -eu

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
CLEAR=0
KERNEL=""
while [ $# -gt 0 ]; do
    case "$1" in
        --clear)  CLEAR=1; shift ;;
        --kernel) KERNEL="${2:?--kernel needs a tarball}"; shift 2 ;;
        *) die "unknown option: $1" ;;
    esac
done
[ -z "$KERNEL" ] || [ -f "$KERNEL" ] || die "no such kernel tarball: $KERNEL"

# The ESP is labelled NETHOSEFI by build-x86.sh and nethos-install.
ESP=""
for v in /Volumes/NETHOSEFI /Volumes/NETHOSEFI[0-9]; do
    [ -d "$v" ] && ESP="$v" && break
done
if [ -z "$ESP" ]; then
    die "no NETHOS ESP mounted.

  Plug the stick into this Mac. macOS mounts the EFI partition as
  /Volumes/NETHOSEFI on its own; if it does not appear, check the stick
  is the one NETHOS was flashed to:  diskutil list external physical"
fi
say "Stick: $ESP"

if [ "$CLEAR" = 1 ]; then
    rm -rf "$ESP/nethos-dev"
    say "Removed $ESP/nethos-dev -- next boot uses the installed payload."
    exit 0
fi

[ -d "$HERE/payload" ] || die "no payload directory beside $0"

DEST="$ESP/nethos-dev"
rm -rf "$DEST"
mkdir -p "$DEST"
# FAT keeps no permissions and no symlinks, which is fine: install-nethos.sh
# sets the modes itself with install -m when it runs on the target.
cp -R "$HERE/payload/." "$DEST/"
# Nothing on a FAT stick should carry a .git or a cache.
find "$DEST" -name '.DS_Store' -delete 2>/dev/null || true

# A kernel change costs a five minute cross-compile; it should not also cost a
# twenty-five minute image build, which only reinstalls 621 unchanged Debian
# packages. The target installs it at boot the same way it applies the payload.
if [ -n "$KERNEL" ]; then
    say "Kernel: $(basename "$KERNEL") ($(du -h "$KERNEL" | cut -f1))"
    cp "$KERNEL" "$DEST/kernel.tgz"
fi

files=$(find "$DEST" -type f | wc -l | tr -d ' ')
bytes=$(du -sh "$DEST" | cut -f1 | tr -d ' ')
say "Copied $files files ($bytes)"
sync

cat <<TXT

Now put the stick back in the machine and boot it. nethos-devsync applies
the payload before the desktop starts, and only when it has changed.

  What it runs:   install-nethos.sh --files-only
  What it logs:   /var/log/nethos-devsync.log   (on the machine)
  To undo:        scripts/dev-push.sh --clear

Carries the shell, the apps, nethosd and the nethos-* tools. With
--kernel it carries a kernel too, installed at boot -- so no change of
any kind needs an image build to reach the machine.
TXT
