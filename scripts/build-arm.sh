#!/bin/bash
# Build an aarch64 NETHOS image, so the VM runs natively on an Apple Silicon
# Mac instead of emulating x86.
#
#     scripts/build-arm.sh
#     scripts/run.sh --arch aarch64
#
# How this works, and why it is not simply "download an ISO":
#
#   Arch Linux is x86_64 only. The ARM port is Arch Linux ARM, a separate
#   community project that ships a root filesystem tarball rather than an
#   installer — there is nothing to boot. Someone has to partition a disk,
#   unpack that tarball onto it and install a bootloader.
#
#   macOS cannot do that: it will not mount ext4 and its tar drops the
#   ownership and permissions a Linux root filesystem depends on. So the work
#   happens inside a throwaway Debian arm64 VM, which runs under HVF at native
#   speed and does the whole thing in minutes.
#
#       Debian arm64 (builder, HVF)
#            └── partitions and formats /dev/vdb
#            └── unpacks Arch Linux ARM onto it
#            └── chroots in and runs install-nethos.sh
#            └── installs GRUB for arm64-efi, then powers off
#
#   What comes out is build/nethos-arm.qcow2, a UEFI-bootable NETHOS disk.
#
# Options:
#   --clean         discard any existing ARM image and start over
#   --size 40G      disk size (default 40G)
#   --keep-builder  leave the builder image in place for a rebuild
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DISK="$BUILD/nethos-arm.qcow2"
BUILDER="$BUILD/debian-arm64-builder.qcow2"
BUILDER_WORK="$BUILD/debian-arm64-work.qcow2"
SEED="$BUILD/seed-arm.iso"
DISK_SIZE="40G"

BUILDER_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"
ALARM_URL="http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"
ALARM_TARBALL="$BUILD/ArchLinuxARM-aarch64-latest.tar.gz"

CLEAN=0
KEEP_BUILDER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1; shift ;;
        --size) DISK_SIZE="${2:?}"; shift 2 ;;
        --keep-builder) KEEP_BUILDER=1; shift ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v qemu-system-aarch64 >/dev/null || die "qemu not installed (brew install qemu)"
command -v qemu-img >/dev/null || die "qemu-img not found"

FW_CODE=""
for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
         /usr/local/share/qemu/edk2-aarch64-code.fd; do
    [ -f "$c" ] && FW_CODE="$c" && break
done
[ -n "$FW_CODE" ] || die "edk2-aarch64-code.fd not found"

ACCEL=tcg
CPU=max
if [ "$(uname -m)" = "arm64" ] && [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
    ACCEL=hvf; CPU=host
else
    say "No HVF on this host — the build will run emulated and take much longer."
fi

mkdir -p "$BUILD"
[ "$CLEAN" -eq 1 ] && rm -f "$DISK"

# ---------------------------------------------------------------- downloads
if [ ! -f "$BUILDER" ]; then
    say "Downloading the Debian arm64 builder image (~430 MB, once)"
    curl -fL --retry 3 -o "$BUILDER" "$BUILDER_URL"
fi
if [ ! -f "$ALARM_TARBALL" ]; then
    say "Downloading Arch Linux ARM aarch64 (~830 MB, once)"
    curl -fL --retry 3 -o "$ALARM_TARBALL" "$ALARM_URL"
fi

# ------------------------------------------------------------------- disks
if [ -f "$DISK" ]; then
    say "ARM image already exists: $DISK  (use --clean to rebuild)"
else
    say "Creating the target disk ($DISK_SIZE)"
    qemu-img create -f qcow2 "$DISK" "$DISK_SIZE" >/dev/null
fi

# The builder is disposable; work on a copy so it can be reused.
rm -f "$BUILDER_WORK"
qemu-img create -f qcow2 -F qcow2 -b "$(cd "$(dirname "$BUILDER")" && pwd)/$(basename "$BUILDER")" \
    "$BUILDER_WORK" >/dev/null
qemu-img resize "$BUILDER_WORK" 12G >/dev/null

FW_VARS="$BUILD/edk2-arm-vars-build.fd"
rm -f "$FW_VARS"
dd if=/dev/zero of="$FW_VARS" bs=1m count=64 2>/dev/null

# -------------------------------------------------------------- seed / plan
say "Staging the build plan and the NETHOS payload"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nethos-arm.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/payload"
cp -R "$ROOT/payload/." "$STAGE/payload/"
# The 830MB rootfs tarball is NOT put on the seed ISO. It was, and the guest
# could not read it back even though macOS could -- rather than fight ISO9660's
# naming and size rules, the tarball is attached as a raw disk and read as a
# stream. No filesystem, nothing to misinterpret, and no second download.

cat > "$STAGE/bootstrap.sh" <<'BOOTSTRAP'
#!/bin/bash
# Runs inside the throwaway Debian builder, as root, with /dev/vdb as the
# target NETHOS disk and /dev/sr0 carrying this script and the payload.
set -euo pipefail
exec > >(tee -a /var/log/nethos-arm-build.log) 2>&1
echo "=== NETHOS ARM build starting $(date -u) ==="

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq parted dosfstools e2fsprogs arch-install-scripts >/dev/null 2>&1 || \
    apt-get install -y -qq parted dosfstools e2fsprogs >/dev/null

SRC=/mnt/src
mkdir -p "$SRC"
mount -o ro /dev/sr0 "$SRC" || { echo "FATAL: cannot mount the seed"; exit 1; }

TARGET=/dev/vdb
echo "--- partitioning $TARGET ---"
wipefs -a "$TARGET"
parted -s "$TARGET" mklabel gpt
parted -s "$TARGET" mkpart ESP fat32 1MiB 513MiB
parted -s "$TARGET" set 1 esp on
parted -s "$TARGET" mkpart root ext4 513MiB 100%
sleep 2
partprobe "$TARGET" || true
sleep 2

mkfs.fat -F32 -n NETHOSEFI "${TARGET}1"
mkfs.ext4 -F -L NETHOS "${TARGET}2"

ROOTFS=/mnt/root
mkdir -p "$ROOTFS"
mount "${TARGET}2" "$ROOTFS"
mkdir -p "$ROOTFS/boot"
mount "${TARGET}1" "$ROOTFS/boot"

echo "--- seed contents ---"
ls -la "$SRC"

echo "--- unpacking Arch Linux ARM from /dev/vdc ---"
# The tarball is a raw disk rather than a file: gzip reads the stream and stops
# at its end, ignoring the disk padding after it. --numeric-owner and -p keep
# the ownership and modes the rootfs ships with; losing them is what makes a
# hand-built Arch root fail to boot.
[ -b /dev/vdc ] || { echo "FATAL: /dev/vdc (rootfs tarball) missing"; lsblk; exit 1; }
mkdir -p "$ROOTFS/dev/pts" "$ROOTFS/proc" "$ROOTFS/sys"
gzip -dc < /dev/vdc 2>/dev/null | tar -xp --numeric-owner -C "$ROOTFS" || true

# Trust the result, not the exit status: a stream that ends in disk padding
# makes tar grumble even when every file landed correctly.
[ -x "$ROOTFS/usr/bin/pacman" ] || { echo "FATAL: rootfs did not unpack"; ls -la "$ROOTFS"; exit 1; }
echo "rootfs unpacked: $(du -sh "$ROOTFS" 2>/dev/null | cut -f1)"

echo "--- preparing the chroot ---"
mount --bind /dev  "$ROOTFS/dev"
mount --bind /dev/pts "$ROOTFS/dev/pts"
mount -t proc  proc  "$ROOTFS/proc"
mount -t sysfs sys   "$ROOTFS/sys"
# The Arch Linux ARM rootfs ships /etc/resolv.conf as a symlink (systemd-
# resolved style). Before the chroot exists, resolving it walks straight
# back out to the *host's* real resolv.conf -- cp then sees source and
# destination as the identical file and refuses. Remove the symlink first
# (rm does not follow it) so cp always writes a fresh regular file.
rm -f "$ROOTFS/etc/resolv.conf"
cp /etc/resolv.conf "$ROOTFS/etc/resolv.conf"

mkdir -p "$ROOTFS/opt/nethos-payload"
cp -r "$SRC/payload/." "$ROOTFS/opt/nethos-payload/"
chmod +x "$ROOTFS/opt/nethos-payload/install-nethos.sh" \
         "$ROOTFS/opt/nethos-payload/bin/"* \
         "$ROOTFS/opt/nethos-payload/nethosd/nethosd.py"

# fstab by UUID so the disk can move between machines and controllers.
ROOT_UUID=$(blkid -s UUID -o value "${TARGET}2")
ROOT_PARTUUID=$(blkid -s PARTUUID -o value "${TARGET}2")
ESP_UUID=$(blkid -s UUID -o value "${TARGET}1")
cat > "$ROOTFS/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /      ext4  rw,relatime  0 1
UUID=$ESP_UUID   /boot  vfat  rw,relatime,fmask=0022,dmask=0022  0 2
FSTAB

cat > "$ROOTFS/root/inside.sh" <<'INSIDE'
#!/bin/bash
set -euo pipefail
echo "--- inside the Arch Linux ARM chroot ---"

# Arch Linux ARM signs with its own keyring.
pacman-key --init
pacman-key --populate archlinuxarm
pacman -Sy --noconfirm archlinuxarm-keyring || true
pacman -Syu --noconfirm

# The generic tarball has no kernel for a UEFI virtual machine; add one, plus
# the firmware bits a bootloader needs.
pacman -S --noconfirm --needed linux-aarch64 linux-firmware \
    grub efibootmgr dosfstools mkinitcpio

# The default ALARM user is in the way of ours.
userdel -r alarm 2>/dev/null || true

# Everything NETHOS: packages, user, shell, autologin, branding.
/opt/nethos-payload/install-nethos.sh

mkinitcpio -P

grub-install --target=arm64-efi --efi-directory=/boot \
             --bootloader-id=NETHOS --removable --no-nvram

# grub-mkconfig's 10_linux script pairs a kernel with its initramfs by
# matching filename patterns (vmlinuz-X <-> initramfs-X.img). Arch Linux
# ARM's linux-aarch64 package ships a bare "Image" with no version suffix,
# so the pairing fails silently and the generated grub.cfg has no `initrd`
# line at all -- confirmed by inspecting it directly. The kernel then boots
# with no initramfs and therefore no udev, so it cannot resolve ANY root=
# identifier that needs a filesystem driver or device scan -- including
# UUID=, which was tried first and produced "VFS: Unable to mount root fs on
# unknown-block(0,0)" even though the UUID itself was correct (verified by
# reading the ext4 superblock straight off the built image). PARTUUID is
# different: it comes from the GPT table, which the kernel parses natively
# at boot with no driver or initramfs involved -- confirmed, it is exactly
# the identifier the kernel's own "available partitions" panic listing
# showed. So: skip grub-mkconfig's autodetection entirely and write a grub.cfg
# by hand, naming the exact two files this build produces.
[ -n "${ROOT_PARTUUID:-}" ] || { echo "FATAL: ROOT_PARTUUID not set"; exit 1; }
[ -f /boot/Image ] || { echo "FATAL: no /boot/Image"; exit 1; }
[ -f /boot/initramfs-linux.img ] || { echo "FATAL: no /boot/initramfs-linux.img"; exit 1; }

mkdir -p /boot/grub
cat > /boot/grub/grub.cfg <<GRUBCFG
set timeout=1
set default=0

menuentry 'NETHOS Linux' {
	linux /Image root=PARTUUID=${ROOT_PARTUUID} rw quiet console=tty0 console=ttyAMA0,115200
	initrd /initramfs-linux.img
}
GRUBCFG

grep -q "root=PARTUUID=${ROOT_PARTUUID}" /boot/grub/grub.cfg && \
grep -q '^\s*initrd /initramfs-linux.img' /boot/grub/grub.cfg || \
    { echo "FATAL: hand-written grub.cfg is missing the linux or initrd line -- would not boot"; exit 1; }

systemctl enable NetworkManager 2>/dev/null || true
# run.sh forwards host port 2222 -> guest 22 for exactly this: SSH in rather
# than drive the VM through the serial console or the GUI window.
systemctl enable sshd 2>/dev/null || true
echo 'root:nethos' | chpasswd
echo "--- chroot finished ---"
INSIDE

chmod +x "$ROOTFS/root/inside.sh"
ROOT_UUID="$ROOT_UUID" ROOT_PARTUUID="$ROOT_PARTUUID" chroot "$ROOTFS" /root/inside.sh
rm -f "$ROOTFS/root/inside.sh"

sync
umount -R "$ROOTFS" || true
umount "$SRC" || true
echo "=== NETHOS ARM build finished $(date -u) ==="
poweroff
BOOTSTRAP

cat > "$STAGE/user-data" <<'USERDATA'
#cloud-config
# Debian's generic cloud image probes EC2 and friends before settling on the
# seed we actually gave it, which costs a couple of minutes of timeouts on a
# machine with no metadata service. Pin it to NoCloud.
datasource_list: [ NoCloud, None ]
datasource:
  NoCloud:
    seedfrom: /mnt/seed/
hostname: nethos-builder
users:
  - name: builder
    lock_passwd: false
    plain_text_passwd: builder
    sudo: ["ALL=(ALL:ALL) NOPASSWD:ALL"]
    shell: /bin/bash
chpasswd:
  expire: false
  list: |
    root:builder
runcmd:
  - [ mkdir, -p, /mnt/seed ]
  - [ mount, -o, ro, /dev/sr0, /mnt/seed ]
  - [ bash, /mnt/seed/bootstrap.sh ]
USERDATA

printf 'instance-id: nethos-arm-build\nlocal-hostname: nethos-builder\n' \
    > "$STAGE/meta-data"

rm -f "$SEED"
hdiutil makehybrid -quiet -iso -joliet -default-volume-name CIDATA \
    -o "$SEED" "$STAGE"
say "Seed: $(du -h "$SEED" | cut -f1)"

# ------------------------------------------------------------------- build
say "Running the builder VM (accel=$ACCEL). This takes a while: it installs a"
say "full Arch Linux ARM plus the NETHOS package set."

CONSOLE_LOG="$BUILD/build-arm-console.log"
rm -f "$CONSOLE_LOG"

# The guest's own bootstrap runs under `set -e` and can die partway (a failed
# command, or the VM being killed from outside) without ever reaching
# `poweroff`. Either way qemu-system-aarch64 still exits and this script would
# carry on to declare success -- it did exactly that once already, on a build
# that died at "preparing the chroot" before pacman or the chroot install ever
# ran. So the console output is captured and checked for the bootstrap's own
# completion line before anything downstream is trusted.
qemu-system-aarch64 \
    -name nethos-arm-builder \
    -machine virt,accel="$ACCEL",highmem=on \
    -cpu "$CPU" \
    -smp "${CPUS:-4}" \
    -m "${MEM:-4096}" \
    -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
    -drive if=pflash,format=raw,file="$FW_VARS" \
    -drive file="$BUILDER_WORK",if=virtio,format=qcow2 \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$ALARM_TARBALL",if=virtio,format=raw,readonly=on \
    -drive file="$SEED",if=none,id=seed,format=raw,media=cdrom,readonly=on \
    -device virtio-scsi-pci -device scsi-cd,drive=seed \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0 \
    -device virtio-rng-pci \
    -nographic 2>&1 | tee "$CONSOLE_LOG"

[ "$KEEP_BUILDER" -eq 1 ] || rm -f "$BUILDER_WORK"

grep -q "=== NETHOS ARM build finished" "$CONSOLE_LOG" || \
    die "the guest build did not finish -- see $CONSOLE_LOG (last lines below)
$(tail -20 "$CONSOLE_LOG")"

say "Built: $DISK"
echo
echo "  Boot it natively on this Mac:"
echo "      scripts/run.sh --arch aarch64"
echo
