#!/bin/bash
# Build the NETHOS installer image.
#
#     scripts/build-installer.sh              online  (~150MB, downloads)
#     scripts/build-installer.sh --offline    offline (~800MB, self-contained)
#
# Two images, because two situations:
#
#   online    a kernel and an initramfs, nothing else. Boots, fetches packages
#             from Debian, installs. Small to download, needs a network.
#
#   offline   the same installer, plus every package it will need on a second
#             partition. For a machine with no network interface, or no driver
#             for the one it has -- which is exactly the machine that cannot
#             download a driver.
#
# Both run the same installer and the same npkg_bootstrap the image build uses,
# so there is one code path and it cannot drift.
#
# Options:
#   --offline        bake the packages in
#   --arch amd64     target architecture (default amd64)
#   --out FILE       where to write the image
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
ARCH="amd64"
OFFLINE=0
OUT=""

while [ $# -gt 0 ]; do
    case "$1" in
        --offline) OFFLINE=1; shift ;;
        --arch) ARCH="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -n "$OUT" ] || OUT="$BUILD/nethos-installer$([ "$OFFLINE" -eq 1 ] && echo -offline).img"

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$BUILD"
LOG="$BUILD/build-installer.log"
exec > >(tee "$LOG") 2>&1

command -v qemu-img >/dev/null || die "qemu-img not found"

# --------------------------------------------------------------------------
# The installer root is built with npkg, the same as everything else. It is the
# "installer" set: busybox, python3-minimal, partitioning tools, curl. No
# systemd -- the installer is PID 1 and reboots when it is done.
say "Building the installer root ($ARCH)"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nethos-inst.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
IROOT="$STAGE/root"
mkdir -p "$IROOT"

python3 -u "$ROOT/pkg/npkg_bootstrap.py" "$IROOT" \
    --set installer --set kernel \
    --arch "$ARCH" --user root --work "$BUILD/installer-work" --keep \
    || die "could not build the installer root"

# The installer itself, and npkg, ride along.
mkdir -p "$IROOT/nethos"
cp -R "$ROOT/pkg" "$IROOT/nethos/pkg"
cp -R "$ROOT/payload" "$IROOT/nethos/payload"
cp "$ROOT/payload/installer/installer.py" "$IROOT/nethos/installer.py"

# A console font, so the framebuffer has something to draw text with. Without
# one the installer still runs and prints to the console instead.
for f in "$IROOT"/usr/share/consolefonts/*.psf* \
         "$IROOT"/usr/share/kbd/consolefonts/*.psf*; do
    [ -f "$f" ] && cp "$f" "$IROOT/nethos/font.psf" && break
done 2>/dev/null || true

# --------------------------------------------------------------------------
# /init. PID 1: mount the kernel filesystems, find the packages if this is an
# offline image, and hand over to the installer.
cat > "$IROOT/init" <<'INIT'
#!/bin/sh
# PID 1 in the initramfs. Nothing else is running.
export PATH=/usr/bin:/usr/sbin:/bin:/sbin

mount -t proc     proc     /proc   2>/dev/null
mount -t sysfs    sysfs    /sys    2>/dev/null
mount -t devtmpfs devtmpfs /dev    2>/dev/null
mount -t tmpfs    tmpfs    /tmp    2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null

# Drivers for disks, network and display. The installer is useless without the
# first two and ugly without the third.
for m in ahci nvme sd_mod usb_storage xhci_pci e1000e r8169 igb virtio_pci \
         virtio_blk virtio_net i915 amdgpu radeon nouveau simpledrm; do
    modprobe "$m" 2>/dev/null
done
sleep 2

# Anything passed on the kernel command line, for unattended installs.
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        nethos.disk=*) export NETHOS_DISK="${arg#nethos.disk=}" ;;
        nethos.user=*) export NETHOS_USER="${arg#nethos.user=}" ;;
        nethos.pass=*) export NETHOS_PASS="${arg#nethos.pass=}" ;;
    esac
done

# Offline images carry their packages on a partition labelled NETHOSPKG. Found
# by label rather than device name, because which disk the installer lands on
# is not knowable in advance.
PKG=$(blkid -L NETHOSPKG 2>/dev/null || true)
if [ -n "$PKG" ]; then
    mkdir -p /nethos/packages
    if mount -o ro "$PKG" /nethos/packages 2>/dev/null; then
        export NETHOS_OFFLINE=/nethos/packages
    fi
fi

# A shell on tty2, so a failed install can be looked at rather than guessed at.
setsid sh -c 'exec sh </dev/tty2 >/dev/tty2 2>&1' &

exec python3 /nethos/installer.py
INIT
chmod +x "$IROOT/init"

# --------------------------------------------------------------------------
say "Packing the initramfs"
KVER=$(ls "$IROOT/usr/lib/modules" 2>/dev/null | head -1)
[ -n "$KVER" ] || die "no kernel in the installer root"

# The kernel goes beside the initramfs, not inside it.
cp "$IROOT/boot/vmlinuz-$KVER" "$STAGE/vmlinuz" 2>/dev/null \
    || cp "$IROOT"/boot/vmlinuz* "$STAGE/vmlinuz"

( cd "$IROOT" && find . -print0 \
    | cpio --null -o --format=newc 2>/dev/null ) \
    | gzip -9 > "$STAGE/initrd.img"

INITRD_MB=$(( $(stat -c%s "$STAGE/initrd.img" 2>/dev/null \
             || stat -f%z "$STAGE/initrd.img") / 1024 / 1024 ))
say "initramfs: ${INITRD_MB}MB"

# --------------------------------------------------------------------------
# Offline: convert every package the target system needs, once, and put them on
# their own partition. The installer then never touches the network.
PKG_MB=0
if [ "$OFFLINE" -eq 1 ]; then
    say "Fetching and converting packages for the offline image"
    PKGDIR="$STAGE/packages"
    mkdir -p "$PKGDIR"
    python3 -u "$ROOT/pkg/npkg_bootstrap.py" "$STAGE/throwaway" \
        --set base --set system --set kernel --set desktop --set firmware \
        --arch "$ARCH" --user neth --work "$PKGDIR-work" --keep --packages-only \
        2>/dev/null || true
    # --packages-only is not implemented yet; until it is, reuse the cache the
    # normal bootstrap leaves behind.
    cp "$PKGDIR-work"/packages/*.npk "$PKGDIR/" 2>/dev/null || \
        die "no converted packages found; run a normal build first so the
cache at $PKGDIR-work is populated"
    PKG_MB=$(( $(du -sm "$PKGDIR" | cut -f1) ))
    say "packages: ${PKG_MB}MB"
fi

# --------------------------------------------------------------------------
say "Assembling $OUT"
ESP_MB=$(( INITRD_MB + 40 ))
TOTAL_MB=$(( ESP_MB + PKG_MB + 40 ))

rm -f "$OUT"
truncate -s "${TOTAL_MB}M" "$OUT"
parted -s "$OUT" mklabel gpt
parted -s "$OUT" mkpart ESP fat32 1MiB "${ESP_MB}MiB"
parted -s "$OUT" set 1 esp on
if [ "$OFFLINE" -eq 1 ]; then
    parted -s "$OUT" mkpart NETHOSPKG ext4 "${ESP_MB}MiB" 100%
fi

say "Built: $OUT (${TOTAL_MB}MB)"
cat <<EOF

  NOT YET FINISHED: the partitions exist but are empty. Populating them needs
  loop devices and root, which means this last step has to run on Linux:

      sudo scripts/build-installer.sh $([ "$OFFLINE" -eq 1 ] && echo --offline)

  Everything before this point -- the installer root, the initramfs, the
  package set -- is built and is in $STAGE.

EOF
