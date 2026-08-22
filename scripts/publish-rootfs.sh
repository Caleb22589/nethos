#!/bin/bash
# Publish a prebuilt NETHOS root, so no machine converts packages to install.
#
#   scripts/publish-rootfs.sh              build the tarball
#   scripts/publish-rootfs.sh --publish    ...and upload it as a release asset
#
# The online installer downloaded .debs and converted all six hundred of them
# on the target. Conversion is xz and zstd decompression and repacking -- pure
# CPU -- and on a two-core 2012 laptop it takes the best part of an hour for
# work that is identical on every machine and has already been done once here.
#
# So do it once. This takes the root the image build already produced, packs
# it, and publishes it; the installer downloads and unpacks, which is I/O and
# a fast decompressor rather than six hundred repack operations.
set -euo pipefail

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HERE/build"
SRC="$BUILD/nethos-x86.qcow2"
RAW="$BUILD/rootfs-src.img"
OUT="$BUILD/nethos-rootfs-x86_64.tar.zst"
TAG="${NETHOS_ROOTFS_TAG:-rootfs-$(date -u +%Y%m%d)}"
PUBLISH=0
[ "${1:-}" = "--publish" ] && PUBLISH=1

[ -f "$SRC" ] || die "no system image at $SRC -- run scripts/build-x86.sh first"
command -v docker >/dev/null || die "docker (colima) is needed to read an ext4 image"

say "Converting $(basename "$SRC") to raw"
rm -f "$RAW"; qemu-img convert -O raw "$SRC" "$RAW"

START=$(dd if="$RAW" bs=1 skip=1184 count=8 2>/dev/null | od -An -tu8 | tr -d ' \n')
[ -n "$START" ] && [ "$START" -gt 0 ] 2>/dev/null || die "no root partition in $RAW"
say "Root at LBA $START"

say "Packing (this is the work every target machine was repeating)"
docker run --rm --privileged -v "$RAW":/d.img -v "$BUILD":/out \
    -e OFF=$((START * 512)) debian:trixie bash -euo pipefail -c '
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq zstd >/dev/null 2>&1
mkdir -p /mnt/r && mount -o loop,offset=$OFF /d.img /mnt/r
echo "    root holds $(du -sm /mnt/r | cut -f1)MB"
# /boot is left out on purpose: the installer partitions and formats its own
# ESP, and the kernel comes from its own release asset. Machine identity is
# left out too -- a root that carries one machine-id makes every install that
# same machine to systemd, DHCP and anything keyed on it.
tar --numeric-owner --xattrs --acls \
    --exclude=./boot/* --exclude=./dev/* --exclude=./proc/* --exclude=./sys/* \
    --exclude=./tmp/* --exclude=./run/* --exclude=./mnt/* --exclude=./media/* \
    --exclude=./var/cache/* --exclude=./var/tmp/* --exclude=./lost+found \
    --exclude=./etc/machine-id --exclude=./etc/fstab \
    --exclude=./etc/ssh/ssh_host_* \
    -C /mnt/r -cf - . | zstd -12 -T0 -q -o /out/nethos-rootfs-x86_64.tar.zst
umount /mnt/r
'
rm -f "$RAW"
[ -f "$OUT" ] || die "no tarball produced"
bytes=$(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT")
say "Built: $OUT  ($(awk -v b="$bytes" 'BEGIN{printf "%.0fMB", b/1048576}'))"

shasum -a 256 "$OUT" | sed "s|$BUILD/||" > "$OUT.sha256"
say "sha256: $(awk '{print $1}' "$OUT.sha256")"

if [ "$PUBLISH" = 1 ]; then
    command -v gh >/dev/null || die "gh is needed to publish"
    say "Publishing as $TAG"
    gh release create "$TAG" "$OUT" "$OUT.sha256" \
        --repo Caleb22589/nethos \
        --title "NETHOS root filesystem ($TAG)" \
        --notes "A prebuilt NETHOS root, so installing does not mean converting six hundred packages on the machine being installed.

nethos-install --online downloads and unpacks this instead of running npkg, which turns an hour of decompression on an old laptop into a download and a tar.

Excludes /boot, machine-id, fstab and ssh host keys: the installer generates those per machine." 2>&1 | tail -2 \
    || gh release upload "$TAG" "$OUT" "$OUT.sha256" --repo Caleb22589/nethos --clobber 2>&1 | tail -2
fi
