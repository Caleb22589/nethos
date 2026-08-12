#!/bin/bash
# Turn a built NETHOS image into something you can flash to a USB stick and
# boot on a real PC.
#
#     scripts/make-usb.sh                 # convert the x86 image
#     scripts/make-usb.sh --arch aarch64  # convert the ARM image
#     scripts/make-usb.sh --write /dev/sdX
#
# This is deliberately not an ISO. An ISO is a read-only filesystem meant for
# an optical disc, and a live ISO carries a squashfs plus an installer that
# copies it onto a disk -- a second build system to write and maintain. What
# build-x86.sh already produces is a *disk*: GPT, an EFI system partition, and
# an ext4 root with GRUB installed. Written to a USB stick byte for byte, that
# stick is a NETHOS installation, and a UEFI machine will boot it directly.
#
# What that means in practice:
#
#   - It boots and runs from the stick, and changes persist. It is not a live
#     image that forgets everything on reboot.
#   - The stick becomes NETHOS: the whole device is overwritten.
#   - Speed is the stick's speed. USB 2.0 will be miserable; USB 3.0 is fine.
#
# To install onto an internal disk instead, write it to that disk the same way
# from any Linux live environment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
ARCH=x86_64
TARGET=""

while [ $# -gt 0 ]; do
    case "$1" in
        --arch) ARCH="${2:?--arch needs x86_64 or aarch64}"; shift 2 ;;
        --write) TARGET="${2:?--write needs a device, e.g. /dev/sdb}"; shift 2 ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

case "$ARCH" in
    x86_64)  SRC="$BUILD/nethos-x86.qcow2"; BUILDER="scripts/build-x86.sh" ;;
    aarch64) SRC="$BUILD/nethos-arm.qcow2"; BUILDER="scripts/build-image.sh" ;;
    *) die "unknown arch: $ARCH" ;;
esac
IMG="$BUILD/nethos-$ARCH.img"

[ -f "$SRC" ] || die "no image at $SRC
Build it first:  $BUILDER"

command -v qemu-img >/dev/null || die "qemu-img not found"

say "Converting $(basename "$SRC") to a raw disk image"
rm -f "$IMG"
qemu-img convert -p -O raw "$SRC" "$IMG"

# Two different numbers, and confusing them wastes real time: the file is
# sparse, so it costs little on disk, but dd reads it end to end and writes
# every empty byte to the stick. What matters for flashing is the apparent
# size, so trim the file to where the partitions actually end.
end=$(partx -g -o END "$IMG" 2>/dev/null | tail -1 | tr -d ' ' || true)
if [ -n "$end" ]; then
    # +34 sectors for the GPT backup header at the end of the disk.
    bytes=$(( (end + 34) * 512 ))
    if [ "$bytes" -gt 0 ] && [ "$bytes" -lt "$(stat -c%s "$IMG" 2>/dev/null || stat -f%z "$IMG")" ]; then
        truncate -s "$bytes" "$IMG"
        command -v sgdisk >/dev/null && sgdisk -e "$IMG" >/dev/null 2>&1 || true
        say "Trimmed to the end of the last partition"
    fi
fi
on_disk=$(du -h "$IMG" | cut -f1)
to_write=$(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1 || echo "$on_disk")
say "Wrote $IMG"
say "  $to_write to flash   ($on_disk actually occupied here -- the file is sparse)"

if [ -z "$TARGET" ]; then
    cat <<EOF

Flash it to a USB stick (the stick is completely overwritten):

  Linux:
    lsblk                          # find the stick: /dev/sdX, NOT a partition
    sudo dd if=$IMG of=/dev/sdX bs=4M status=progress conv=fsync

  macOS:
    diskutil list                  # find the stick: /dev/diskN
    diskutil unmountDisk /dev/diskN
    sudo dd if=$IMG of=/dev/rdiskN bs=4m status=progress

  Or use Balena Etcher, which accepts a .img and is harder to point at the
  wrong disk.

Then boot the PC from it with UEFI. Disable Secure Boot: this GRUB is not
signed, so a machine with Secure Boot on will refuse it without explanation.

  Log in:  neth / nethos          (root also nethos)

EOF
    exit 0
fi

# --write was given: check it looks like a whole disk rather than a partition,
# and make the user say yes to the specific device.
case "$TARGET" in
    *[0-9]) die "$TARGET looks like a partition. Write the whole disk instead." ;;
esac
[ -b "$TARGET" ] || die "$TARGET is not a block device"

echo
say "About to ERASE $TARGET and write NETHOS to it:"
lsblk -o NAME,SIZE,MODEL,MOUNTPOINT "$TARGET" 2>/dev/null || true
echo
printf 'Type the device path again to confirm: '
read -r confirm
[ "$confirm" = "$TARGET" ] || die "not confirmed; nothing was written"

say "Writing (this takes a while)"
sudo dd if="$IMG" of="$TARGET" bs=4M status=progress conv=fsync
sync
say "Done. Boot it with UEFI, Secure Boot off."
