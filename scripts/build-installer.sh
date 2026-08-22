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

echo "--- busybox applets ---"
# Debian ships busybox as one binary and creates almost no applet symlinks, so
# udhcpc exists inside it and cannot be called by name. It decides which applet
# to be from argv[0], so a symlink is all it needs. dhclient would be the
# alternative and pulls a daemon and a lease database to get an address once.
if [ -x "$R/bin/busybox" ] || [ -x "$R/usr/bin/busybox" ]; then
    bb=/bin/busybox; [ -x "$R/usr/bin/busybox" ] && bb=/usr/bin/busybox
    # reboot, poweroff and halt are applets too, and the installer offers to
    # reboot when it finishes. "reboot: not found" at the end of a successful
    # install is a poor last impression.
    for applet in udhcpc reboot poweroff halt shutdown; do
        [ -e "$R/usr/sbin/$applet" ] || ln -sf "$bb" "$R/usr/sbin/$applet"
    done
    # udhcpc does nothing with a lease unless a script applies it, and Debian
    # puts one here. Without it the address is negotiated and never set.
    if [ ! -x "$R/usr/share/udhcpc/default.script" ]; then
        mkdir -p "$R/usr/share/udhcpc"
        cat > "$R/usr/share/udhcpc/default.script" <<'"'"'DHCP'"'"'
#!/bin/sh
[ -n "$1" ] || exit 1
case "$1" in
    deconfig) ip addr flush dev "$interface" 2>/dev/null; ip link set "$interface" up ;;
    bound|renew)
        # busybox udhcpc exports "subnet", not "mask". Using the wrong name
        # gave ip an address with an empty prefix, which it rejected -- so the
        # lease was obtained, no address was ever set, and the installer said
        # "Associated" and then quietly went back to the menu.
        ip addr flush dev "$interface" 2>/dev/null
        ip addr add "$ip${subnet:+/$subnet}" dev "$interface" 2>/dev/null
        ip link set "$interface" up 2>/dev/null
        for r in $router; do
            ip route add default via "$r" dev "$interface" 2>/dev/null && break
        done
        : > /etc/resolv.conf
        for s in $dns; do echo "nameserver $s" >> /etc/resolv.conf; done
        [ -n "$domain" ] && echo "search $domain" >> /etc/resolv.conf
        ;;
esac
exit 0
DHCP
        chmod 755 "$R/usr/share/udhcpc/default.script"
    fi
    echo "    udhcpc linked to busybox"
fi

echo "--- alternatives ---"
# awk, vi, sh and friends are symlinks Debian creates in a maintainer script,
# and npkg runs none -- so gawk is installed and /usr/bin/awk does not exist.
# This is the whole class of bug that CLAUDE.md warns about, and it presents
# here as an installer whose every awk pipeline silently produces nothing.
# Follow a symlink chain inside the root. Absolute links resolve against the
# host from out here, and Debian points /usr/bin/awk at /etc/alternatives/awk
# which points at /usr/bin/gawk -- two hops, either of which can dangle when no
# maintainer script has run.
# Does this program exist and point at something real, inside the root?
#
# Chasing the symlink chain does not work here: /usr/bin/busybox links to
# /bin/busybox and /bin links back to usr/bin, so following it bounces between
# the two until any hop limit is reached and reports a program that is present
# and working as missing. Absolute links resolve against the host as well.
#
# So do not chase it. A name is satisfied if a real executable file of that
# name exists in any of the bin directories, or if the name is a symlink whose
# target basename is such a file. That answers the only question that
# matters -- will the shell find something to run -- without walking a graph
# that contains a cycle by design.
resolve_in_root() {   # $1 path relative to $R
    _n=$(basename "$1")
    for _d in usr/bin usr/sbin bin sbin; do
        [ -f "$R/$_d/$_n" ] && [ ! -L "$R/$_d/$_n" ] && [ -x "$R/$_d/$_n" ] && return 0
    done
    for _d in usr/bin usr/sbin bin sbin; do
        [ -L "$R/$_d/$_n" ] || continue
        _t=$(basename "$(readlink "$R/$_d/$_n")")
        for _e in usr/bin usr/sbin bin sbin; do
            [ -f "$R/$_e/$_t" ] && [ ! -L "$R/$_e/$_t" ] && [ -x "$R/$_e/$_t" ] && return 0
        done
    done
    return 1
}

link_alt() {   # $1 name, then candidates
    n=$1; shift
    # Present and working is fine; present and dangling is worse than absent,
    # because it looks installed.
    if [ -e "$R/usr/bin/$n" ] && resolve_in_root "usr/bin/$n" >/dev/null; then
        return 0
    fi
    [ -e "$R/usr/bin/$n" ] && { rm -f "$R/usr/bin/$n"; echo "    $n was dangling"; }
    for c in "$@"; do
        if [ -x "$R/usr/bin/$c" ]; then
            ln -sf "$c" "$R/usr/bin/$n"
            echo "    $n -> $c"
            return 0
        fi
    done
}

link_alt awk gawk mawk original-awk busybox
[ -e "$R/usr/bin/awk" ] && : || echo "    no awk candidate; have:" $(ls "$R"/usr/bin/*awk* "$R"/bin/*awk* 2>/dev/null)
link_alt vi vim.tiny vim busybox
link_alt pager less more

echo "--- checking the installer can actually run ---"
# Every one of these is called by nethos-installer or nethos-install, and a
# missing one does not announce itself: "ip: not found" scrolled past inside a
# diagnostic and the installer reported a wifi card whose firmware had loaded
# perfectly as refusing to come up. Cheaper to assert here than to flash a
# stick and read it off a photograph.
missing=""
for prog in ip iw wpa_supplicant udhcpc reboot lsblk sgdisk mkfs.ext4 mkfs.fat \
            lspci dmesg awk sed nl sort cut python3 tar zstd xz curl \
            grub-install chroot rsync; do
    found=""
    for d in usr/bin usr/sbin bin sbin; do
        resolve_in_root "$d/$prog" >/dev/null 2>&1 && found=1 && break
    done
    [ -n "$found" ] || missing="$missing $prog"
done
if [ -n "$missing" ]; then
    echo "FATAL: the installer image is missing:$missing"
    for prog in $missing; do
        echo "  $prog:"
        ls -l "$R"/usr/bin/"$prog" "$R"/usr/sbin/"$prog" "$R"/bin/"$prog" "$R"/sbin/"$prog" 2>/dev/null | sed "s|$R||;s|^|    |"
    done
    echo "  busybox is at:" $(ls "$R"/bin/busybox "$R"/usr/bin/busybox 2>/dev/null | sed "s|$R||")
    echo "  Add the package that provides each to the installer set in"
    echo "  pkg/npkg_bootstrap.py. An installer that cannot run its own tools"
    echo "  fails on the machine, where the error is hardest to read."
    exit 1
fi
echo "    all present"

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
# Python multiprocessing creates its semaphores in /dev/shm, and npkg converts
# packages in parallel. Without it: "parallel conversion unavailable: no such
# file or directory, using one core" -- the install still works and takes
# several times longer than it needs to.
mkdir -p /dev/shm && mount -t tmpfs -o mode=1777,nosuid,nodev shm /dev/shm 2>/dev/null
# devpts, so anything wanting a pty inside the install has one.
mkdir -p /dev/pts && mount -t devpts -o gid=5,mode=620 devpts /dev/pts 2>/dev/null
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
# 4MB of slack, not 24. FAT needs room for its tables and GRUB, and nothing
# else is ever written here. The extra was invisible in du, which reports
# blocks actually allocated on a sparse file -- but dd writes the length, so
# it was twenty megabytes of zeroes going to the stick every time.
KB=$(( ( $(stat -c%s /out/installer-vmlinuz) + $(stat -c%s /out/installer-initrd.zst) ) / 1024 + 4096 ))
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
bytes=$(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT")
say "Built: $OUT"
say "  $(awk -v b="$bytes" 'BEGIN{printf "%.0fMB", b/1048576}') to flash (what dd writes, not what du reports)"
echo
echo "Flash it (the whole stick is overwritten):"
echo "  diskutil list external physical"
echo "  diskutil unmountDisk /dev/diskN"
echo "  sudo dd if=$OUT of=/dev/rdiskN bs=4m status=progress"
