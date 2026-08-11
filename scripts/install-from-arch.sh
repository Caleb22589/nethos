#!/bin/bash
# Install NETHOS onto a real machine, from the official Arch Linux live USB.
#
# This is the direct route to bare metal. You do NOT need to build a NETHOS ISO
# first: boot the official Arch ISO, clone this repository, run this script.
#
#   ./scripts/install-from-arch.sh --list                 show disks, change nothing
#   ./scripts/install-from-arch.sh --target /dev/sda -n   dry run, change nothing
#   ./scripts/install-from-arch.sh --target /dev/sda      install
#
# Options:
#   --hostname NAME   system hostname            (default nethos)
#   --user NAME       desktop user               (default neth)
#   --timezone ZONE   e.g. Pacific/Auckland      (default UTC)
#   --ref REF         git ref of NETHOS to install (default: this checkout)
#   --no-confirm      skip the typed confirmation
#   --allow-loop      permit a loop device as target (used by the test suite)
#
# THIS ERASES THE TARGET DISK COMPLETELY — partition table, filesystems, other
# operating systems, all data. There is no undo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"

TARGET=""
HOSTNAME_NEW="nethos"
NETH_USER="neth"
TIMEZONE="UTC"
DRY_RUN=0
CONFIRM=1
LIST=0
ALLOW_LOOP=0

while [ $# -gt 0 ]; do
    case "$1" in
        --target|-t) TARGET="${2:?}"; shift 2 ;;
        --hostname) HOSTNAME_NEW="${2:?}"; shift 2 ;;
        --user) NETH_USER="${2:?}"; shift 2 ;;
        --timezone) TIMEZONE="${2:?}"; shift 2 ;;
        --dry-run|-n) DRY_RUN=1; shift ;;
        --no-confirm) CONFIRM=0; shift ;;
        --list|-l) LIST=1; shift ;;
        --allow-loop) ALLOW_LOOP=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"
command -v pacstrap >/dev/null || die "pacstrap missing — run this from the Arch live ISO
  (or: pacman -S arch-install-scripts)"

# --------------------------------------------------------------------------
# disks
# --------------------------------------------------------------------------
live_disk() {
    local src
    src=$(findmnt -no SOURCE /run/archiso/bootmnt 2>/dev/null || true)
    [ -n "$src" ] && lsblk -no PKNAME "$src" 2>/dev/null | head -1
}
LIVE_DISK="$(live_disk || true)"

if [ "$LIST" -eq 1 ] || [ -z "$TARGET" ]; then
    say "Disks on this machine"
    printf '%-14s %-9s %s\n' DEVICE SIZE MODEL
    lsblk -dno NAME,SIZE,TYPE,MODEL | while read -r name size type model; do
        [ "$type" = disk ] || continue
        case "$name" in loop*|ram*|sr*) continue ;; esac
        note=""
        [ "$name" = "$LIVE_DISK" ] && note="   <- live USB, not a target"
        printf '/dev/%-9s %-9s %s%s\n' "$name" "$size" "${model:-—}" "$note"
    done
    [ -z "$TARGET" ] && printf '\nThen:  %s --target /dev/sdX -n\n\n' "$0"
    exit 0
fi

[ -b "$TARGET" ] || die "not a block device: $TARGET"
TARGET_NAME="$(basename "$TARGET")"
TARGET_TYPE="$(lsblk -dno TYPE "$TARGET")"

if [ "$TARGET_TYPE" != disk ]; then
    if [ "$TARGET_TYPE" = loop ] && [ "$ALLOW_LOOP" -eq 1 ]; then
        warn "target is a loop device — test mode"
    else
        die "$TARGET is type '$TARGET_TYPE', not a whole disk (pass /dev/sda, not /dev/sda1)"
    fi
fi

[ -n "$LIVE_DISK" ] && [ "$TARGET_NAME" = "$LIVE_DISK" ] \
    && die "$TARGET is the live USB you booted from"

if lsblk -no MOUNTPOINTS "$TARGET" | grep -qE '\S'; then
    lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS "$TARGET"
    die "$TARGET has mounted partitions — unmount them first"
fi

if [ -d /sys/firmware/efi ]; then FIRMWARE=UEFI; else FIRMWARE=BIOS; fi

# --------------------------------------------------------------------------
# packages
# --------------------------------------------------------------------------
# The NETHOS package set lives in one place; strip comments and blank lines.
NETHOS_PKGS="$(grep -vE '^\s*(#|$)' "$REPO/iso/packages.nethos" | tr '\n' ' ')"

# Bare metal needs the things a cloud image already had: a kernel, firmware for
# real devices, CPU microcode, and a way to get on Wi-Fi.
BASE_PKGS="base linux linux-firmware intel-ucode amd-ucode \
networkmanager sudo nano vim grub efibootmgr"

say "Install plan"
cat <<EOF

  Target disk : $TARGET  ($(lsblk -dno SIZE "$TARGET")$(lsblk -dno MODEL "$TARGET" | sed 's/^ *//;s/ *$//;s/^/, /'))
  Firmware    : $FIRMWARE
  Hostname    : $HOSTNAME_NEW
  User        : $NETH_USER   (password: nethos — change it at first login)
  Timezone    : $TIMEZONE
  NETHOS from : $REPO ($(git -C "$REPO" log -1 --format=%h 2>/dev/null || echo 'local'))

  Layout      : GPT, erasing everything on $TARGET
EOF
if [ "$FIRMWARE" = UEFI ]; then
    echo "                p1  1G    EFI System (FAT32) -> /boot"
else
    echo "                p1  1M    BIOS boot partition"
fi
cat <<EOF
                p2  rest  ext4 -> /

  Current contents:
EOF
lsblk -o NAME,SIZE,FSTYPE,LABEL "$TARGET" | sed 's/^/    /'
echo

if [ "$DRY_RUN" -eq 1 ]; then
    say "Dry run — nothing changed."
    exit 0
fi

warn "This ERASES $TARGET completely."
if [ "$CONFIRM" -eq 1 ]; then
    printf '\nType ERASE %s to continue: ' "$TARGET"
    read -r reply
    [ "$reply" = "ERASE $TARGET" ] || die "not confirmed — nothing changed"
fi

# --------------------------------------------------------------------------
say "Partitioning"
# --------------------------------------------------------------------------
umount -R /mnt 2>/dev/null || true
wipefs -a "$TARGET"
sgdisk --zap-all "$TARGET"
if [ "$FIRMWARE" = UEFI ]; then
    sgdisk -n1:0:+1G -t1:ef00 -c1:"NETHOS EFI" "$TARGET"
else
    sgdisk -n1:0:+1M -t1:ef02 -c1:"BIOS boot" "$TARGET"
fi
sgdisk -n2:0:0 -t2:8300 -c2:"NETHOS root" "$TARGET"
partprobe "$TARGET" 2>/dev/null || true
sleep 2

case "$TARGET" in
    *nvme*|*mmcblk*|*loop*) P1="${TARGET}p1"; P2="${TARGET}p2" ;;
    *)                      P1="${TARGET}1";  P2="${TARGET}2"  ;;
esac
[ -b "$P2" ] || die "expected partition $P2 did not appear"

say "Creating filesystems"
[ "$FIRMWARE" = UEFI ] && mkfs.fat -F32 -n NETHOSEFI "$P1"
mkfs.ext4 -F -L NETHOS "$P2"

mount "$P2" /mnt
if [ "$FIRMWARE" = UEFI ]; then
    mkdir -p /mnt/boot
    mount "$P1" /mnt/boot
fi

# --------------------------------------------------------------------------
say "Installing packages (the long step — downloads ~1GB)"
# --------------------------------------------------------------------------
# shellcheck disable=SC2086
pacstrap -K /mnt $BASE_PKGS $NETHOS_PKGS

say "Generating fstab"
genfstab -U /mnt >> /mnt/etc/fstab

# --------------------------------------------------------------------------
say "Applying the NETHOS layer"
# --------------------------------------------------------------------------
# Ship the repository along so the installed system can update itself later.
mkdir -p /mnt/usr/share/nethos-payload
cp -r "$REPO/payload/." /mnt/usr/share/nethos-payload/
chmod +x /mnt/usr/share/nethos-payload/install-nethos.sh \
         /mnt/usr/share/nethos-payload/bin/* \
         /mnt/usr/share/nethos-payload/nethosd/nethosd.py

cat > /mnt/root/nethos-chroot.sh <<CHROOT
#!/bin/bash
set -euo pipefail

ln -sf "/usr/share/zoneinfo/$TIMEZONE" /etc/localtime
hwclock --systohc 2>/dev/null || true

sed -i 's/^#en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen
locale-gen
echo 'LANG=en_US.UTF-8' > /etc/locale.conf

echo "$HOSTNAME_NEW" > /etc/hostname
cat > /etc/hosts <<HOSTS
127.0.0.1   localhost
::1         localhost
127.0.1.1   $HOSTNAME_NEW.localdomain $HOSTNAME_NEW
HOSTS

# The NETHOS layer: user, autologin, shell, SDK, apps, branding. Packages are
# already in place from pacstrap, so skip that step.
NETH_USER="$NETH_USER" /usr/share/nethos-payload/install-nethos.sh --no-packages

# Networking on real hardware, including Wi-Fi.
systemctl enable NetworkManager

mkinitcpio -P

if [ -d /sys/firmware/efi ]; then
    # --removable also writes the fallback path, which is what many consumer
    # firmwares actually look for.
    grub-install --target=x86_64-efi --efi-directory=/boot \
                 --bootloader-id=NETHOS --removable
    grub-install --target=x86_64-efi --efi-directory=/boot \
                 --bootloader-id=NETHOS 2>/dev/null || true
else
    grub-install --target=i386-pc "$TARGET"
fi
sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=2/' /etc/default/grub
sed -i 's/^GRUB_DISTRIBUTOR=.*/GRUB_DISTRIBUTOR="NETHOS"/' /etc/default/grub
grub-mkconfig -o /boot/grub/grub.cfg

echo 'root:nethos' | chpasswd
CHROOT

chmod +x /mnt/root/nethos-chroot.sh
arch-chroot /mnt /root/nethos-chroot.sh
rm -f /mnt/root/nethos-chroot.sh

say "Unmounting"
sync
umount -R /mnt

say "NETHOS is installed on $TARGET"
cat <<EOF

  Remove the USB stick and reboot.

  Log in as $NETH_USER / nethos, then IMMEDIATELY change both passwords:
      passwd
      sudo passwd root

  Wi-Fi:      nmtui
  Updates:    nethos-update
  New app:    nethos-app new myapp "My App"

EOF
