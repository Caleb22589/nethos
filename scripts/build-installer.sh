#!/bin/bash
# Build the lightweight NETHOS installer: a kernel and an initramfs, nothing else.
#
#   scripts/build-installer.sh
#
# The system image is ten gigabytes and takes minutes to flash. Nobody should
# wait for that to install an operating system that then downloads its packages
# anyway. This produces something around a hundred megabytes that flashes in
# seconds, boots on anything with UEFI, and builds the real system onto the
# disk from the archive.
#
# Everything lives in the initramfs. No root partition, no squashfs to mount,
# no root= to get wrong -- GRUB loads two files and the kernel has its whole
# world in RAM. On unknown hardware that is several fewer ways to fail.
#
# What it deliberately does not carry: ath11k firmware (63MB on its own) and
# the full mediatek set (45MB). They are a download away once the machine is
# online, and carrying them would double the image.
set -euo pipefail

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HERE/build"
OUT="$BUILD/nethos-installer.img"
KERNEL_TARBALL="${NETHOS_INSTALLER_KERNEL:-$HOME/builds/installer/linux-7.2.0-x86_64.tar.gz}"
SYSTEM_KERNEL="${NETHOS_SYSTEM_KERNEL:-$HOME/builds/linux-7.2.0-x86_64.tar.gz}"

[ -f "$KERNEL_TARBALL" ] || die "no installer kernel at $KERNEL_TARBALL
  Build one:  nethos-kernel build --cross --profile installer --out ~/builds/installer"
command -v docker >/dev/null || die "docker (colima) is needed to assemble a Linux root"

mkdir -p "$BUILD"
say "Installer kernel: $(basename "$KERNEL_TARBALL") ($(du -h "$KERNEL_TARBALL" | cut -f1))"

# The whole assembly happens in one Linux container: npkg needs a Linux host to
# unpack into, mksquashfs and cpio are Linux tools, and grub-install writes an
# EFI binary. macOS can do none of it.
docker run --rm --privileged \
    -v "$HERE":/nethos:ro \
    -v "$BUILD":/out \
    -v "$KERNEL_TARBALL":/kernel.tgz:ro \
    -v "$SYSTEM_KERNEL":/system-kernel.tgz:ro \
    -w /work debian:trixie bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
# grub-efi-amd64-bin does not exist for arm64 and asking for it failed the
# whole build with a bare exit 100, because this was silenced. The x86_64 GRUB
# modules come from the amd64 root npkg builds below; grub-common here only
# provides grub-mkimage, which is architecture-independent.
apt-get install -y -qq python3 cpio zstd dosfstools mtools \
    grub-common gdisk curl xz-utils

R=/work/root
mkdir -p "$R"

echo "--- bootstrapping the installer root ---"
# Only what an installer uses: partition a disk, reach a network, run npkg.
# No desktop, no browser, no X, no sound.
python3 -u /nethos/pkg/npkg_bootstrap.py "$R" \
    --set base --set installer --set net --set firmware --arch amd64 --user root --work /work/cache --keep

echo "--- the kernel it boots with ---"
tar xzf /kernel.tgz -C "$R"
KVER=$(ls -1 "$R/lib/modules" 2>/dev/null | sort -V | tail -1)
[ -n "$KVER" ] || { echo "FATAL: no kernel in the tarball"; exit 1; }
echo "    $KVER"
cp "$R/boot/vmlinuz-$KVER" /out/installer-vmlinuz
rm -f "$R/boot/vmlinux-$KVER"

# The system kernel is not carried.
#
# It is 43MB, which is nearly half the budget for the whole image, and this
# installer exists to be small. The installed system gets its kernel from the
# archive like everything else; nethos-kernel build --profile system replaces
# it with 7.2 and BORE afterwards, on a machine that by then has a working
# network and a compiler.

echo "--- npkg and the payload, so the target can be built and dressed ---"
mkdir -p "$R/usr/share/nethos/pkg"
cp -R /nethos/pkg/. "$R/usr/share/nethos/pkg/"
cp -R /nethos/payload "$R/usr/share/nethos/payload"
for t in /nethos/payload/bin/*; do
    [ -f "$t" ] && install -m755 "$t" "$R/usr/bin/$(basename "$t")"
done

echo "--- trimming ---"
before=$(du -sm "$R" | cut -f1)
rm -rf "$R"/usr/share/doc "$R"/usr/share/man "$R"/usr/share/info \
       "$R"/usr/share/locale "$R"/var/cache/* "$R"/usr/share/zoneinfo/right \
       "$R"/usr/lib/python3*/test "$R"/usr/lib/python3*/lib2to3 2>/dev/null || true
# Firmware is the single largest thing an installer carries, and most of it is
# for hardware that cannot be installing anything. Keep what a laptop needs to
# get online; the rest is a download away once it has.
FW="$R/usr/lib/firmware"
if [ -d "$FW" ]; then
    before_fw=$(du -sm "$FW" | cut -f1)
    ( cd "$FW"
      # Keep the chips that get a laptop online, and only those. Everything
      # else is a download away once it is -- which is the only situation in
      # which any of it matters.
      #
      # Kept whole because they are small and cover the machines this is for:
      # rtw89 (RTL8852, the HP Envy), brcm (the MacBooks), rtw88, ath9k.
      # iwlwifi is kept under a size cap: it is eighty-odd files and the modern
      # AX blobs are several megabytes each, which alone would double the
      # image. The cap keeps the older and mid-range Intel parts -- including
      # the Latitude Centrino -- and drops the newest, which any machine new
      # enough to have one can fetch.
      for d in *; do
        case "$d" in
          rtw88|rtw89|brcm|ath9k_htc|rtl_nic) ;;
          # regulatory.db is a symlink to regulatory.db-debian, and deleting
          # the target left a dangling link that the kernel reports as
          # "Direct firmware load for regulatory.db failed with error -2" --
          # indistinguishable from the file never having been there. Without
          # it cfg80211 falls back to a world domain that refuses most
          # channels, which looks like a radio that finds nothing.
          regulatory.db*) ;;
          iwlwifi-*) [ "$(stat -c%s "$d")" -gt 2200000 ] && rm -f "$d" ;;
          *) rm -rf "$d" ;;
        esac
      done
      find . -type f -size +6M -delete 2>/dev/null || true
    )
    echo "    firmware: ${before_fw}MB -> $(du -sm "$FW" | cut -f1)MB (wifi to get online; the rest downloads)"
fi
# The kernel only ever searches /lib/firmware.
[ -d "$R/usr/lib/firmware" ] && [ ! -e "$R/lib/firmware" ] && ln -s ../usr/lib/firmware "$R/lib/firmware"
echo "    ${before}MB -> $(du -sm "$R" | cut -f1)MB"

echo "--- where the size is ---"
echo "  biggest files:"; find "$R" -type f -size +4M -printf "%s %p\n" 2>/dev/null \
  | sort -rn | head -6 | awk "{printf \"    %6.1fMB %s\\n\", \$1/1048576, \$2}" | sed "s|$R||"
du -sm "$R"/usr/lib/firmware "$R"/usr/lib/python3* "$R"/usr/share/nethos \
       "$R"/usr/lib/x86_64-linux-gnu "$R"/usr/bin "$R"/usr/sbin "$R"/lib/modules 2>/dev/null \
  | sort -rn | head -8 | sed "s|$R|  |"

echo "--- init ---"
# No systemd. An installer has one job and running it directly removes every
# unit-ordering problem between power-on and a prompt.
cat > "$R/init" <<INIT
#!/bin/sh
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mount -t tmpfs tmp /tmp 2>/dev/null
mkdir -p /run /var/run && mount -t tmpfs run /run 2>/dev/null
# Bring interfaces up and try DHCP on anything wired; the installer offers wifi.
for i in \$(ls /sys/class/net 2>/dev/null); do
    [ "\$i" = lo ] && continue
    ip link set "\$i" up 2>/dev/null
done
ip link set lo up 2>/dev/null
(udevd --daemon 2>/dev/null || /lib/systemd/systemd-udevd --daemon 2>/dev/null) || true
udevadm trigger 2>/dev/null || true
udevadm settle 2>/dev/null || true
(dhclient -nw 2>/dev/null || udhcpc -b 2>/dev/null) || true
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export NETHOS_INSTALLER_MEDIA=1
exec setsid sh -c "exec nethos-installer </dev/tty1 >/dev/tty1 2>&1" || exec sh
INIT
chmod 755 "$R/init"

echo "--- initramfs ---"
( cd "$R" && find . -print0 | cpio --null -o -H newc --quiet ) \
    | zstd -19 -T0 -q -o /out/installer-initrd.zst
ls -l /out/installer-initrd.zst | awk "{print \"    initrd: \" \$5 \" bytes\"}"

echo "--- EFI image ---"
KB=$(( ( $(stat -c%s /out/installer-vmlinuz) + $(stat -c%s /out/installer-initrd.zst) ) / 1024 + 24576 ))
rm -f /out/nethos-installer.img
truncate -s "${KB}K" /out/nethos-installer.img
mkfs.vfat -F 32 -n NETHOSINST /out/nethos-installer.img >/dev/null
mmd -i /out/nethos-installer.img ::/EFI ::/EFI/BOOT ::/boot 2>/dev/null || true
# -d points at the amd64 modules npkg fetched, not the arm64 ones here.
grub-mkimage -d "$R/usr/lib/grub/x86_64-efi" -O x86_64-efi -o /tmp/bootx64.efi -p /boot/grub \
    part_gpt part_msdos fat ext2 normal linux echo all_video search \
    search_label search_fs_uuid configfile loadenv test keystatus
mmd -i /out/nethos-installer.img ::/boot/grub 2>/dev/null || true
cat > /tmp/grub.cfg <<GRUBCFG
set timeout=3
set default=0
menuentry "Install NETHOS" {
    linux /boot/vmlinuz console=tty0 quiet loglevel=3
    initrd /boot/initrd.zst
}
menuentry "Install NETHOS (verbose)" {
    linux /boot/vmlinuz console=tty0
    initrd /boot/initrd.zst
}
GRUBCFG
mcopy -i /out/nethos-installer.img /tmp/bootx64.efi ::/EFI/BOOT/BOOTX64.EFI
mcopy -i /out/nethos-installer.img /tmp/grub.cfg ::/boot/grub/grub.cfg
mcopy -i /out/nethos-installer.img /out/installer-vmlinuz ::/boot/vmlinuz
mcopy -i /out/nethos-installer.img /out/installer-initrd.zst ::/boot/initrd.zst
rm -f /out/installer-vmlinuz /out/installer-initrd.zst
echo "--- done ---"
'

[ -f "$OUT" ] || die "no image produced"
say "Built: $OUT  ($(du -h "$OUT" | cut -f1))"
echo
echo "Flash it (the whole stick is overwritten):"
echo "  diskutil list external physical"
echo "  diskutil unmountDisk /dev/diskN"
echo "  sudo dd if=$OUT of=/dev/rdiskN bs=4m status=progress"
