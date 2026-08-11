#!/bin/bash
# Build a bootable NETHOS live ISO.
#
# MUST run on an Arch Linux x86_64 machine as root — including inside the
# NETHOS VM itself, which is exactly what it was developed against:
#
#     sudo pacman -S --needed archiso
#     sudo ./iso/build-iso.sh
#
# Produces a hybrid ISO that boots on both UEFI and legacy BIOS machines, boots
# straight into the NETHOS desktop, and carries `nethos-install` for putting it
# on a real disk.
#
# Options:
#   --out DIR        where to write the ISO (default ./out)
#   --work DIR       scratch directory (default /var/tmp/nethos-iso)
#   --compress MODE  zstd (default, fast) | xz (smaller, much slower)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/out"
WORK=/var/tmp/nethos-iso
COMPRESS=zstd

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="${2:?}"; shift 2 ;;
        --work) WORK="${2:?}"; shift 2 ;;
        --compress) COMPRESS="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,18p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root (mkarchiso needs it)"
command -v mkarchiso >/dev/null || die "archiso not installed: pacman -S archiso"
[ -d /usr/share/archiso/configs/releng ] || die "archiso profiles missing"

PROFILE="$WORK/profile"
rm -rf "$PROFILE"
mkdir -p "$PROFILE" "$OUT"

# ---------------------------------------------------------------------------
say "Starting from the stock archiso releng profile"
# ---------------------------------------------------------------------------
# Copying releng rather than vendoring it means the bootloader configs always
# match the installed archiso version, which is the part most likely to break
# if it were pinned in this repository.
cp -r /usr/share/archiso/configs/releng/. "$PROFILE/"

# ---------------------------------------------------------------------------
say "Applying the NETHOS profile"
# ---------------------------------------------------------------------------
# Identify the ISO as NETHOS. profiledef.sh drives the label, the volume name
# and the compression settings.
sed -i \
    -e 's/^iso_name=.*/iso_name="nethos"/' \
    -e 's/^iso_label=.*/iso_label="NETHOS_$(date +%Y%m)"/' \
    -e 's/^iso_publisher=.*/iso_publisher="NETHOS"/' \
    -e 's/^iso_application=.*/iso_application="NETHOS Live"/' \
    "$PROFILE/profiledef.sh"

if [ "$COMPRESS" = "zstd" ]; then
    # releng defaults to xz, which is punishing on an emulated CPU and slow
    # even on real hardware. zstd costs a few hundred MB and saves hours.
    python3 - "$PROFILE/profiledef.sh" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path).read()
src = re.sub(r"airootfs_image_type=.*", 'airootfs_image_type="squashfs"', src)
src = re.sub(
    r"airootfs_image_tool_options=\([^)]*\)",
    "airootfs_image_tool_options=('-comp' 'zstd' '-Xcompression-level' '8' '-b' '1M')",
    src,
)
open(path, "w").write(src)
PY
fi

# --- packages --------------------------------------------------------------
cat "$REPO/iso/packages.nethos" >> "$PROFILE/packages.x86_64"
say "Package list: $(grep -cvE '^\s*(#|$)' "$PROFILE/packages.x86_64") packages"

# --- the NETHOS payload ----------------------------------------------------
# The whole repository payload rides along inside the ISO. First boot applies
# it with install-nethos.sh --no-packages, which is fast because every package
# it needs is already in the image.
install -d "$PROFILE/airootfs/usr/share/nethos-payload"
cp -r "$REPO/payload/." "$PROFILE/airootfs/usr/share/nethos-payload/"

# --- first-boot unit -------------------------------------------------------
install -d "$PROFILE/airootfs/etc/systemd/system"
cp "$REPO/iso/nethos-live-setup.service" \
   "$PROFILE/airootfs/etc/systemd/system/nethos-live-setup.service"

install -d "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants"
ln -sf ../nethos-live-setup.service \
   "$PROFILE/airootfs/etc/systemd/system/multi-user.target.wants/nethos-live-setup.service"

# archiso's releng autologins root on tty1 via this override. The NETHOS
# installer replaces it with an autologin for the neth user, so drop it here to
# avoid two conflicting definitions of the same unit.
rm -rf "$PROFILE/airootfs/etc/systemd/system/getty@tty1.service.d"

# --- executable bits -------------------------------------------------------
# squashfs keeps what the profile declares, not what is on the build host, so
# every script has to be listed explicitly or it lands non-executable.
python3 - "$PROFILE/profiledef.sh" <<'PY'
import re, sys
path = sys.argv[1]
src = open(path).read()
entries = [
    '  ["/usr/share/nethos-payload/install-nethos.sh"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-session"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-menu-toggle"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-reload"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-update"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-app"]="0:0:755"',
    '  ["/usr/share/nethos-payload/bin/nethos-install"]="0:0:755"',
    '  ["/usr/share/nethos-payload/nethosd/nethosd.py"]="0:0:755"',
]
block = "file_permissions=(\n" + "\n".join(entries) + "\n"
src = re.sub(r"file_permissions=\(\n", block, src, count=1)
open(path, "w").write(src)
PY

# ---------------------------------------------------------------------------
say "Building (this is the long part — squashfs is CPU bound)"
# ---------------------------------------------------------------------------
rm -rf "$WORK/tmp"
mkarchiso -v -w "$WORK/tmp" -o "$OUT" "$PROFILE"

ISO="$(ls -t "$OUT"/*.iso 2>/dev/null | head -1)"
[ -n "$ISO" ] || die "mkarchiso finished but produced no ISO"

say "Built: $ISO ($(du -h "$ISO" | cut -f1))"
sha256sum "$ISO" | tee "$ISO.sha256"
say "Flash it with:  sudo dd if=$ISO of=/dev/sdX bs=4M status=progress oflag=sync"
