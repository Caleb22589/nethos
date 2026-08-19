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

# Refuse a disk that was never built -- read the label, do not guess at it.
#
# build-x86.sh can stop before it ever boots the builder -- a missing kernel
# tarball, no network, a full disk -- and it leaves behind the empty qcow2 it
# created at the start. Converting that produces a full-size image that flashes
# perfectly and boots to nothing, which is a worse failure than an error here
# because it is only discovered at the machine it was meant for.
#
# The first version of this check grepped the first megabyte for "NETHOS" and
# rejected every good image. Nothing in the first megabyte says NETHOS: the GPT
# names its partitions "ESP" and "root", and the NETHOS label is in the ext4
# superblock of partition 2, half a gigabyte in. So find the partition the way
# the partition table describes it and read the label out of the superblock.
#
# GPT: entry array at LBA 2, 128 bytes per entry, starting LBA 32 bytes into
# the entry. Partition 2 is therefore at byte 1024 + 128 + 32.
# ext4: superblock 1024 bytes into the partition, volume label at 0x78, 16 long.
# Two different answers, because they mean opposite things. No partition table
# at all is the empty qcow2 this check exists to catch, and is fatal. A table
# that is there but whose label will not read is this check being out of its
# depth, and must not stop a build that is probably fine.
root_start() {
    local start
    start=$(dd if="$1" bs=1 skip=1184 count=8 2>/dev/null | od -An -tu8 | tr -d ' \n')
    case "$start" in ''|*[!0-9]*) return 1 ;; esac
    [ "$start" -gt 0 ] 2>/dev/null || return 1
    printf '%s' "$start"
}

root_label() {
    dd if="$1" bs=1 skip=$(( $2 * 512 + 1024 + 120 )) count=16 2>/dev/null | tr -d '\0'
}

say "Converting $(basename "$SRC") to a raw disk image"
rm -f "$IMG"
qemu-img convert -p -O raw "$SRC" "$IMG"

# A check that cannot read the image says so and gets out of the way. This one
# has already blocked a perfectly good build once by being confidently wrong,
# and an unflashable image is a worse outcome than an unbuilt one reaching dd.
if ! START=$(root_start "$IMG"); then
    rm -f "$IMG"
    die "$(basename "$SRC") does not contain a built system.

  It has no partition table at all, so the builder never wrote to it --
  check the output of build-x86.sh for where it stopped. Flashing this
  would give you a stick that boots to nothing.

  Re-run:  ./scripts/build-x86.sh"
fi

LABEL=$(root_label "$IMG" "$START")
if [ -z "$LABEL" ]; then
    say "  note: partitioned, but the root label would not read; continuing"
elif [ "$LABEL" != "NETHOS" ]; then
    rm -f "$IMG"
    die "$(basename "$SRC") does not contain a built system.

  Its root partition is labelled '$LABEL', not NETHOS. Flashing this would
  give you a stick that boots to nothing.

  Re-run:  ./scripts/build-x86.sh"
else
    say "  root partition: NETHOS"
fi

# Shrink to what is actually used, then let it grow again on first boot.
#
# The image is a 10G disk holding about 2G, and dd does not care that the rest
# is zeroes -- it writes every byte. Telling the user to "rebuild smaller" was
# useless advice, because the disk was already the smaller size; the space is
# free space *inside* the filesystem, which only a filesystem resize can
# reclaim.
#
# So: shrink the root filesystem to its minimum, shrink the partition to match,
# truncate the file, and move the GPT backup header to the new end. The stick
# then gets ~2G written instead of 10G, and nethos-growroot expands the
# filesystem to fill the disk on first boot -- so nothing is lost.
#
# Needs root and loop devices, so it only runs on Linux. Everywhere else the
# image is written as-is, which is correct, just slower.
# Which tool is missing, by name. "needs one of four things" is not a
# diagnostic, it is a shrug -- and it cost a round trip to find out that the
# only absentee was sgdisk.
SHRINK_WHY=""
shrink_possible() {
    [ "$(uname -s)" = "Linux" ] || { SHRINK_WHY="not Linux"; return 1; }
    for t in losetup resize2fs dumpe2fs partx; do
        command -v "$t" >/dev/null || { SHRINK_WHY="$t is not installed"; return 1; }
    done
    # sfdisk (util-linux, always present) or sgdisk (gptfdisk, often not).
    command -v sfdisk >/dev/null || command -v sgdisk >/dev/null \
        || { SHRINK_WHY="neither sfdisk nor sgdisk is installed"; return 1; }
    return 0
}

shrink_image() {
    shrink_possible || return 1

    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

    local loop start blocks bsize fs_bytes end_sector
    loop=$($SUDO losetup --find --show -P "$IMG") || return 1
    # Everything from here must release the loop device, however it exits.
    trap '$SUDO losetup -d "$loop" 2>/dev/null || true' RETURN

    $SUDO e2fsck -fy "${loop}p2" >/dev/null 2>&1 || true
    $SUDO resize2fs -M "${loop}p2" >/dev/null 2>&1 || return 1

    blocks=$($SUDO dumpe2fs -h "${loop}p2" 2>/dev/null \
             | awk -F: '/Block count/  {gsub(/ /,"",$2); print $2}')
    bsize=$($SUDO dumpe2fs -h "${loop}p2" 2>/dev/null \
             | awk -F: '/Block size/   {gsub(/ /,"",$2); print $2}')
    [ -n "$blocks" ] && [ -n "$bsize" ] || return 1
    fs_bytes=$(( blocks * bsize ))

    $SUDO losetup -d "$loop" 2>/dev/null || true
    trap - RETURN

    start=$(partx -g -o START -n 2 "$IMG" 2>/dev/null | tr -d ' ')
    [ -n "$start" ] || return 1
    # 8MB of slack so the filesystem is not wedged exactly against the end.
    end_sector=$(( start + (fs_bytes + 511) / 512 + 16384 ))

    # Rewrite partition 2 at its new size. The filesystem UUID is untouched by
    # resize2fs, and GRUB boots by filesystem UUID, so this cannot break it.
    #
    # Truncate first, then write the table: sfdisk puts the GPT backup header
    # at the end of the file it is given, so doing it in this order leaves a
    # correct table rather than one pointing past the end.
    truncate -s $(( (end_sector + 34) * 512 )) "$IMG" || return 1

    if command -v sfdisk >/dev/null; then
        local size=$(( end_sector - start + 1 ))
        $SUDO sfdisk --dump "$IMG" 2>/dev/null \
            | sed -E "s|^(.*img2 : start=[[:space:]]*[0-9]+, size=)[[:space:]]*[0-9]+|\\1 $size|" \
            > "$IMG.table" || return 1
        # If the dump did not name the partition as expected, do not guess.
        grep -q "size= *$size" "$IMG.table" || { rm -f "$IMG.table"; return 1; }
        $SUDO sfdisk --no-reread --force "$IMG" < "$IMG.table" >/dev/null 2>&1 \
            || { rm -f "$IMG.table"; return 1; }
        rm -f "$IMG.table"
    else
        $SUDO sgdisk -d 2 -n "2:${start}:${end_sector}" -t 2:8300 -c 2:root "$IMG" \
            >/dev/null 2>&1 || return 1
        $SUDO sgdisk -e "$IMG" >/dev/null 2>&1 || true
    fi
    return 0
}

before=$(stat -c%s "$IMG" 2>/dev/null || stat -f%z "$IMG")
say "Shrinking to the data actually in it"
if shrink_image; then
    after=$(stat -c%s "$IMG" 2>/dev/null || stat -f%z "$IMG")
    say "  $(( before / 1048576 ))MB -> $(( after / 1048576 ))MB to write"
    say "  the filesystem grows back to fill the disk on first boot"
else
    say "  skipped: ${SHRINK_WHY:-the resize did not complete}"
    say "  the image still works; dd will just write its empty space too"
fi

on_disk=$(du -h "$IMG" | cut -f1)
to_write=$(du -h --apparent-size "$IMG" 2>/dev/null | cut -f1 || echo "$on_disk")
say "Wrote $IMG"
say "  $to_write to flash"

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

Only the used data is written -- the filesystem was shrunk to fit it. On first
boot it grows again to fill whatever it landed on, so a 2G write on a 256G SSD
still gives you the whole 256G.

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
