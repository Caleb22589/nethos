#!/bin/bash
# Build a bootable NETHOS x86-64 disk image from converted Debian packages.
#
#     scripts/build-x86.sh
#     scripts/run.sh --arch x86_64
#
# The work happens inside a throwaway Debian amd64 VM, because it has to happen
# as root: setuid bits survive a tarball but file *ownership* does not, and a
# sudo owned by anyone but root refuses to run. macOS also cannot mount ext4,
# so there is nowhere to install to from the host.
#
#     Debian amd64 (builder, root)
#          ├── partitions /dev/vdb: ESP + ext4 root
#          ├── npkg-bootstrap: resolve, download, convert, install
#          ├── chroot: initramfs, machine-id, fstab, default target
#          └── GRUB for x86_64-efi, then powers off
#
# On an Apple Silicon Mac this runs under TCG (software emulation) and is
# significantly slower than the ARM build. On a Linux x86_64 host with KVM it
# runs at near-native speed.
#
# Options:
#   --clean          start from an empty disk
#   --size 16G       disk size (default 10G; a build with a custom kernel needs it)
#   --user NAME      the account to create (default neth)
#   --sets "a b"     package sets. Default is everything for real hardware:
#                    "base system kernel desktop firmware browser".
#                    Leaner options:
#                      drop "browser"   ~400MB, no Chromium (the shell is WebKit)
#                      drop "firmware"  ~300MB, fine in a VM, not on hardware
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DISK="$BUILD/nethos-x86.qcow2"
BUILDER="$BUILD/debian-amd64-builder.qcow2"
BUILDER_WORK="$BUILD/debian-amd64-work.qcow2"
# The package cache, and the one disk here that deliberately outlives a build.
# The builder overlay is thrown away and recreated every run, so anything on it
# -- including several hundred megabytes of downloaded .deb -- was being fetched
# again from scratch every single time. This holds them across builds; a rebuild
# with no version changes downloads nothing at all.
CACHE="$BUILD/nethos-pkgcache-x86.qcow2"
SEED="$BUILD/seed-x86.iso"
BUILDER_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2"

DISK_SIZE="10G"
DISK_SIZE_SET=0
# Megabytes for the ESP. It holds a kernel and an initrd for BOTH A/B slots,
# so it is not merely a boot stub -- and a mainline initramfs is many times the
# size of a distro one. NETHOS_ESP_MB overrides.
ESP_MB="${NETHOS_ESP_MB:-512}"
ESP_MB_SET=0
# An `a && b` statement that fails is fatal under set -e, so this is an if.
if [ -n "${NETHOS_ESP_MB:-}" ]; then ESP_MB_SET=1; fi
USERNAME="neth"
SETS="base system kernel desktop firmware browser"
CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1; shift ;;
        --size) DISK_SIZE="${2:?}"; DISK_SIZE_SET=1; shift 2 ;;
        --user) USERNAME="${2:?}"; shift 2 ;;
        --sets) SETS="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# Always keep a log next to the images, so there is somewhere to look without
# having to remember how the build was launched.
mkdir -p "$BUILD"
LOG="$BUILD/build-x86.log"
exec > >(tee "$LOG") 2>&1

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v qemu-system-x86_64 >/dev/null || die "qemu not found (brew install qemu)"

FW_CODE=""
for c in /opt/homebrew/share/qemu/edk2-x86_64-code.fd \
         /usr/local/share/qemu/edk2-x86_64-code.fd \
         /usr/share/qemu/edk2-x86_64-code.fd \
         /usr/share/OVMF/OVMF_CODE.fd \
         /usr/share/edk2-ovmf/x64/OVMF_CODE.fd \
         /usr/share/edk2/x64/OVMF_CODE.4m.fd \
         /usr/share/edk2/x64/OVMF_CODE.fd \
         /usr/share/edk2/OVMF_CODE_4M.fd \
         /usr/share/edk2/ovmf/OVMF_CODE.fd; do
    [ -f "$c" ] && FW_CODE="$c" && break
done
if [ -z "$FW_CODE" ]; then
    # Say what is actually missing. Every distribution puts this somewhere
    # different -- Arch moved it to /usr/share/edk2/x64/OVMF_CODE.4m.fd -- so
    # "not found" on its own sends people hunting for a package they already
    # have installed.
    found=$(find /usr/share -maxdepth 4 -iname 'OVMF_CODE*.fd' 2>/dev/null | head -5)
    if [ -n "$found" ]; then
        die "UEFI firmware found but at an unexpected path:
$found
Add it to the search list in $0, or symlink it to /usr/share/edk2/x64/OVMF_CODE.4m.fd"
    fi
    die "No UEFI firmware (OVMF_CODE.fd) on this machine.
  Arch/CachyOS:   sudo pacman -S edk2-ovmf
  Debian/Ubuntu:  sudo apt install ovmf
  macOS:          brew install qemu"
fi

# Acceleration: KVM on Linux x86, HVF on macOS x86, TCG everywhere else.
ACCEL=tcg; CPU=Nehalem
if [ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] && [ -w /dev/kvm ]; then
    ACCEL=kvm; CPU=host
elif [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "x86_64" ] && \
     [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
    ACCEL=hvf; CPU=host
fi

if [ "$ACCEL" = "tcg" ] && [ "$(uname -m)" = "arm64" ]; then
    say "WARNING: x86_64 under TCG on Apple Silicon — this will be slow."
    say "The ARM build (scripts/build-image.sh) runs at native speed on this Mac."
fi

# Falling back to TCG on a machine that has KVM is the difference between a
# four minute build and an hour, and the usual cause is a permission rather
# than a missing feature: /dev/kvm is root:kvm 0660 on Arch and Debian, so a
# user outside the kvm group fails the -w test above and gets emulation with
# no indication that anything is wrong.
if [ "$ACCEL" = "tcg" ] && [ "$(uname -s)" = "Linux" ] && \
   [ "$(uname -m)" = "x86_64" ]; then
    if [ -e /dev/kvm ]; then
        say "WARNING: /dev/kvm exists but is not writable by $(id -un)."
        say "         Running under TCG emulation, roughly 20x slower."
        say "         Fix:  sudo usermod -aG kvm $(id -un)   (then log out and back in)"
        say "         Or run this script with sudo."
    else
        say "WARNING: no /dev/kvm on this machine; running under TCG emulation."
        say "         Check virtualisation is enabled in the firmware."
    fi
fi

# The builder gets four cores regardless of the host, which wastes most of a
# machine like a 5950X: conversion is the long pole and it scales across cores.
# Take half the host's threads, within reason, and enough memory to match --
# npkg extracts several packages at once and /tmp is usually a tmpfs.
HOST_CPUS=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
if [ "$ACCEL" = "tcg" ]; then
    VM_CPUS=4                      # emulation does not scale; more just thrashes
    VM_MEM=4096
else
    VM_CPUS=$(( HOST_CPUS / 2 )); [ "$VM_CPUS" -lt 4 ] && VM_CPUS=4
    [ "$VM_CPUS" -gt 16 ] && VM_CPUS=16
    VM_MEM=$(( VM_CPUS * 1024 )); [ "$VM_MEM" -lt 4096 ] && VM_MEM=4096
    [ "$VM_MEM" -gt 16384 ] && VM_MEM=16384
fi
say "Builder: accel=$ACCEL, ${VM_CPUS} vCPU, ${VM_MEM}MB (host has $HOST_CPUS threads)"

mkdir -p "$BUILD"
[ "$CLEAN" -eq 1 ] && rm -f "$DISK"

if [ ! -f "$BUILDER" ]; then
    say "Downloading the Debian amd64 builder (~460 MB, once)"
    # Download beside it, then move. curl -o writes in place, so a stopped
    # download leaves a truncated qcow2 that looks cached -- and every later
    # run boots it and fails with "failure reading sector N from hd0", which
    # names neither the download nor the file. Interrupting a 460MB fetch is
    # normal; being unable to build afterwards should not be.
    curl -fL --retry 3 -o "$BUILDER.part" "$BUILDER_URL" || {
        rm -f "$BUILDER.part"
        die "could not download the builder image"
    }
    # A qcow2 qemu-img cannot read is not a qcow2 worth keeping.
    if ! qemu-img info "$BUILDER.part" >/dev/null 2>&1; then
        rm -f "$BUILDER.part"
        die "the downloaded builder image is not a readable qcow2 -- try again"
    fi
    mv "$BUILDER.part" "$BUILDER"
fi

# A supplied kernel needs far more room than the modules themselves take.
#
# mkinitramfs decompresses every .ko.zst into a staging directory before it
# packs them, so a mainline tree built from a distro config -- four thousand
# modules against Debian's eight hundred -- needs several gigabytes that exist
# nowhere in the finished image. That space is transient, and make-usb.sh
# shrinks the filesystem before flashing, so a larger disk costs nothing on the
# stick. This has to be decided before the disk is created, not when the
# tarball is staged further down.
# Size the image to the kernel that is going in it, not to the worst kernel
# ever handed to it.
#
# A tarball built from a distro config is gigabytes and produces an initrd too
# big for a normal ESP; one built from defconfig plus the fragment is tens of
# megabytes and needs none of that headroom. Both are legitimate inputs, so
# measure rather than assume: on macOS especially, where make-usb.sh cannot
# shrink the filesystem, every gigabyte of disk is a gigabyte written to the
# stick.
if [ -n "${NETHOS_KERNEL_TARBALL:-}" ] && [ -f "${NETHOS_KERNEL_TARBALL:-}" ]; then
    TARBALL_MB=$(( $(wc -c < "$NETHOS_KERNEL_TARBALL") / 1048576 ))
    say "Kernel tarball: ${TARBALL_MB}MB"
    if [ "$TARBALL_MB" -gt 300 ]; then
        if [ "$DISK_SIZE_SET" = 0 ]; then
            DISK_SIZE="20G"
            say "  large kernel: using a ${DISK_SIZE} disk (override with --size)"
        fi
        if [ "$ESP_MB_SET" = 0 ]; then
            ESP_MB=2048
            say "  large kernel: using a ${ESP_MB}MB ESP (override with NETHOS_ESP_MB)"
        fi
    fi
fi
ESP_END=$(( ESP_MB + 1 ))

say "Creating the target disk ($DISK_SIZE)"
rm -f "$DISK"
qemu-img create -f qcow2 "$DISK" "$DISK_SIZE" >/dev/null

rm -f "$BUILDER_WORK"
# Verify the cached builder every time, not only when it is downloaded: it may
# have been truncated by an earlier interrupted run, or by a full disk.
if ! qemu-img info "$BUILDER" >/dev/null 2>&1; then
    die "the cached builder image is damaged:
  $BUILDER
Delete it and run this again -- it will be downloaded afresh."
fi

qemu-img create -f qcow2 -F qcow2 -b "$BUILDER" "$BUILDER_WORK" >/dev/null
qemu-img resize "$BUILDER_WORK" 16G >/dev/null

# Created once and then kept. Deleting it only costs a re-download.
if [ ! -f "$CACHE" ]; then
    say "Creating the package cache (kept between builds; rm $CACHE to reset)."
    qemu-img create -f qcow2 "$CACHE" 24G >/dev/null
fi

FW_VARS="$BUILD/edk2-x86-vars-build.fd"
rm -f "$FW_VARS"
# x86_64 pflash has an 8 MB combined limit (code + vars), so we cannot create a
# 64 MB zeroed file the way the ARM build does. Copy the shipped template, which
# is already the right size (~540 KB).
FW_VARS_TEMPLATE=""
# The variables template must be the one that matches the code file. Arch's
# OVMF_CODE.4m.fd is 4MB and pairs with OVMF_VARS.4m.fd; pairing it with a 2MB
# VARS gives a machine that reaches the UEFI shell and never boots, which is a
# long way to travel for a mismatched file size. Look beside the code file
# first, then fall back to the general list.
FW_VARS_TEMPLATE=""
case "$FW_CODE" in
    *OVMF_CODE.4m.fd)  cand="${FW_CODE%OVMF_CODE.4m.fd}OVMF_VARS.4m.fd" ;;
    *OVMF_CODE_4M.fd)  cand="${FW_CODE%OVMF_CODE_4M.fd}OVMF_VARS_4M.fd" ;;
    *OVMF_CODE.fd)     cand="${FW_CODE%OVMF_CODE.fd}OVMF_VARS.fd" ;;
    *edk2-x86_64-code.fd) cand="${FW_CODE%edk2-x86_64-code.fd}edk2-i386-vars.fd" ;;
    *) cand="" ;;
esac
[ -n "$cand" ] && [ -f "$cand" ] && FW_VARS_TEMPLATE="$cand"

for t in /opt/homebrew/share/qemu/edk2-i386-vars.fd \
         /usr/local/share/qemu/edk2-i386-vars.fd \
         /usr/share/qemu/edk2-i386-vars.fd \
         /usr/share/OVMF/OVMF_VARS.fd \
         /usr/share/edk2-ovmf/x64/OVMF_VARS.fd \
         /usr/share/edk2/x64/OVMF_VARS.4m.fd \
         /usr/share/edk2/OVMF_VARS_4M.fd; do
    [ -n "$FW_VARS_TEMPLATE" ] && break
    [ -f "$t" ] && FW_VARS_TEMPLATE="$t" && break
done
say "Firmware: $(basename "$FW_CODE") + $(basename "${FW_VARS_TEMPLATE:-none}")"
if [ -n "$FW_VARS_TEMPLATE" ]; then
    cp "$FW_VARS_TEMPLATE" "$FW_VARS"
else
    # Last resort: create a vars file the same size as the code file (the two
    # pflash devices must have the same size on q35).
    CODE_SIZE=$(stat -f%z "$FW_CODE" 2>/dev/null || stat -c%s "$FW_CODE")
    dd if=/dev/zero of="$FW_VARS" bs=1 count="$CODE_SIZE" 2>/dev/null
fi

# --------------------------------------------------------------------------
say "Staging npkg and the build plan"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nethos-x86.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/pkg"
cp "$ROOT"/pkg/*.py "$STAGE/pkg/"
# The shell rides along: npkg_bootstrap looks for it beside the pkg directory.
cp -R "$ROOT/payload" "$STAGE/payload"

# NETHOS_KERNEL_TARBALL=/path/to/linux-7.2.0-x86.tar.gz  ./scripts/build-x86.sh
# Baked into the image rather than installed afterwards, for when the machine
# being flashed should arrive already running it.
if [ -n "${NETHOS_KERNEL_TARBALL:-}" ]; then
    [ -f "$NETHOS_KERNEL_TARBALL" ] || die "no such kernel tarball: $NETHOS_KERNEL_TARBALL"
    cp "$NETHOS_KERNEL_TARBALL" "$STAGE/custom-kernel.tgz"
    say "Baking in $(basename "$NETHOS_KERNEL_TARBALL")"
fi

cat > "$STAGE/build.sh" <<BOOTSTRAP
#!/bin/bash
# Runs as root inside the builder. /dev/vdb is the target disk.
set -euo pipefail
exec > >(tee -a /var/log/nethos-x86.log) 2>&1
echo "=== NETHOS x86 image build starting \$(date -u) ==="

USERNAME="$USERNAME"
SETS="$SETS"
ESP_END="$ESP_END"
WANT_CUSTOM_KERNEL="$([ -n "${NETHOS_KERNEL_TARBALL:-}" ] && echo 1 || echo 0)"
BOOTSTRAP
cat >> "$STAGE/build.sh" <<'BOOTSTRAP'

export DEBIAN_FRONTEND=noninteractive

# A failed build must still power the VM down. Without this the builder sits at
# a login prompt holding a write lock on the target and cache disks, and the
# next build dies instantly with 'Failed to get "write" lock' -- which looks
# like a corrupt image rather than a leftover process.
trap 'st=$?; echo "=== NETHOS x86 image build FAILED (status $st) ==="; sync; poweroff -f' ERR

# Debian's cloud image fires unattended-upgrades and apt-daily on first boot,
# and they hold the apt lock. Without this the build blocks on the lock at 0%
# CPU forever, which looks exactly like a hang.
echo "--- quieting Debian's background apt jobs ---"
systemctl stop apt-daily.service apt-daily-upgrade.service \
    unattended-upgrades.service 2>/dev/null || true
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
pkill -9 -f unattended-upgrade 2>/dev/null || true
# Belt and braces: wait for the lock rather than failing if something else
# grabbed it first.
APT="apt-get -o DPkg::Lock::Timeout=600 -y -qq"

echo "--- installing build tools ---"
$APT update
$APT install parted dosfstools e2fsprogs python3 >/dev/null
echo "--- build tools ready ---"

SRC=/mnt/src
mkdir -p "$SRC"
mount -o ro /dev/sr0 "$SRC" || { echo "FATAL: cannot mount seed"; exit 1; }

# /dev/vdc is the package cache, and the only disk that survives a build. It is
# mounted before the bootstrap so downloads and converted packages land on it
# instead of on the builder overlay, which is deleted the moment we power off.
WORK=/var/tmp/nethos-work
mkdir -p "$WORK"
if ! blkid /dev/vdc >/dev/null 2>&1; then
    echo "--- formatting the package cache (first build) ---"
    mkfs.ext4 -q -F -L NETHOSCACHE /dev/vdc
fi
if mount /dev/vdc "$WORK"; then
    echo "cache: $(du -sh "$WORK" 2>/dev/null | cut -f1) already present"
else
    echo "WARNING: package cache would not mount; downloading everything fresh"
fi

TARGET=/dev/vdb
echo "--- partitioning ---"
wipefs -a "$TARGET"
parted -s "$TARGET" mklabel gpt
parted -s "$TARGET" mkpart ESP fat32 1MiB "${ESP_END:-513}MiB"
parted -s "$TARGET" set 1 esp on
parted -s "$TARGET" mkpart root ext4 "${ESP_END:-513}MiB" 100%
sleep 2; partprobe "$TARGET" || true; sleep 2

mkfs.fat -F32 -n NETHOSEFI "${TARGET}1"
mkfs.ext4 -q -F -L NETHOS "${TARGET}2"

R=/mnt/target
mkdir -p "$R"
mount "${TARGET}2" "$R"
mkdir -p "$R/boot"
# The ESP is mounted before the bootstrap so the kernel package lands its
# vmlinuz straight onto it, with no copying afterwards.
mount "${TARGET}1" "$R/boot"

echo "--- bootstrapping (as root, so ownership and setuid are real) ---"
python3 -u "$SRC/pkg/npkg_bootstrap.py" "$R" \
    $(for s in $SETS; do printf -- '--set %s ' "$s"; done) \
    --arch amd64 --user "$USERNAME" --work "$WORK" --keep

echo "--- preparing the chroot ---"
mkdir -p "$R/dev/pts" "$R/proc" "$R/sys" "$R/run"
mount --bind /dev "$R/dev"
mount --bind /dev/pts "$R/dev/pts"
mount -t proc proc "$R/proc"
mount -t sysfs sys "$R/sys"
mount -t tmpfs tmpfs "$R/run"
cp /etc/resolv.conf "$R/etc/resolv.conf"

ROOT_UUID=$(blkid -s UUID -o value "${TARGET}2")
ESP_UUID=$(blkid -s UUID -o value "${TARGET}1")
cat > "$R/etc/fstab" <<FSTAB
UUID=$ROOT_UUID  /      ext4  rw,relatime  0 1
UUID=$ESP_UUID   /boot  vfat  rw,relatime,fmask=0022,dmask=0022  0 2
FSTAB

cat > "$R/root/inside.sh" <<'INSIDE'
#!/bin/bash
set -euo pipefail
# set -e aborts with no message at all, which turns any mistake in here into
# "the build stopped after the last thing it printed" and nothing more. Say
# which line died and what it was running.
trap 'echo "FATAL: inside.sh line $LINENO failed (status $?): $BASH_COMMAND"' ERR
echo "--- inside the NETHOS x86 root ---"

# systemd refuses to boot without a machine-id; empty is the documented way to
# say "generate one on first boot".
: > /etc/machine-id
mkdir -p /etc/systemd/system /etc/systemd/network /etc/tmpfiles.d /etc/modules-load.d
ln -sfn /usr/lib/systemd/system/multi-user.target /etc/systemd/system/default.target

# /sbin/init is what the kernel executes. Our layout merges sbin into bin, so
# make sure the name it looks for resolves.
if [ ! -e /usr/bin/init ] && [ -e /usr/lib/systemd/systemd ]; then
    ln -sf /usr/lib/systemd/systemd /usr/bin/init
fi

# Users that systemd's own units expect to exist. sshd is here for the same
# reason as the rest: its privilege-separation account is created by openssh-
# server's postinst, and without it sshd refuses to start at all.
for u in systemd-network:998 systemd-resolve:997 systemd-timesync:996 sshd:74; do
    name=${u%:*}; id=${u#*:}
    grep -q "^$name:" /etc/passwd || \
        echo "$name:x:$id:$id:$name:/:/usr/bin/nologin" >> /etc/passwd
    grep -q "^$name:" /etc/group || echo "$name:x:$id:" >> /etc/group
done

ldconfig || true

# GSettings schemas ship as .gschema.xml and are useless until compiled into a
# single gschemas.compiled -- normally by libglib2.0-0's postinst, which we do
# not run. GLib treats the absence as fatal ("No GSettings schemas are
# installed on the system") and kills the process, so parts of the desktop die
# on startup while the schema files sit right there on disk.
if command -v glib-compile-schemas >/dev/null && \
   [ -d /usr/share/glib-2.0/schemas ]; then
    glib-compile-schemas /usr/share/glib-2.0/schemas || true
    if [ -f /usr/share/glib-2.0/schemas/gschemas.compiled ]; then
        echo "gschemas.compiled: $(ls -l /usr/share/glib-2.0/schemas/gschemas.compiled | awk '{print $5}') bytes"
    else
        echo "WARNING: GSettings schemas did not compile; the shell may not start"
    fi
fi

# ca-certificates ships the CAs as individual files and builds the trust store
# in its postinst. Without that step /etc/ssl/certs is unpopulated and every
# TLS client reports "System trust contains zero trusted certificates" -- so
# npkg fetch over https, and the news and stock widgets, all fail.
if command -v update-ca-certificates >/dev/null; then
    # update-ca-certificates trusts what /etc/ca-certificates.conf lists, and
    # that file is written by ca-certificates' postinst (through debconf), not
    # shipped in the .deb. Without it the command runs, reports nothing wrong,
    # and produces an empty trust store -- so every https client fails with
    # "System trust contains zero trusted certificates" while the certificates
    # themselves sit unused in /usr/share/ca-certificates.
    # update-ca-certificates cds into /etc/ssl/certs and dies if it is not
    # there. The directory comes from the ca-certificates postinst, not the
    # .deb, so on a root we assembled ourselves it simply does not exist:
    #   /usr/sbin/update-ca-certificates: 122: cd: can't cd to /etc/ssl/certs
    mkdir -p /etc/ssl/certs /usr/local/share/ca-certificates
    if [ -d /usr/share/ca-certificates ] && [ ! -s /etc/ca-certificates.conf ]; then
        ( cd /usr/share/ca-certificates && find . -name '*.crt' | sed 's|^\./||' | sort ) \
            > /etc/ca-certificates.conf
        echo "ca-certificates.conf: $(wc -l < /etc/ca-certificates.conf) certificates listed"
    fi
    # Not silenced: hiding this is why an empty trust store looked like a
    # success for so long.
    update-ca-certificates --fresh 2>&1 | tail -2 || true
    certs=$( (find /etc/ssl/certs -maxdepth 1 -name '*.0' 2>/dev/null || true) | wc -l )
    bundle=/etc/ssl/certs/ca-certificates.crt
    if [ -s "$bundle" ]; then
        echo "ca-certificates: $certs hashed, bundle $(wc -c < "$bundle") bytes"
    else
        echo "WARNING: no CA bundle at $bundle; https will fail"
    fi
    if [ "$certs" -eq 0 ]; then
        echo "WARNING: no CA certificates; https will fail"
    fi
fi

# The rest of the caches Debian builds from postinsts and dpkg triggers. None
# of these are fatal on their own, which is what makes them worth doing here:
# each one is instead paid for at runtime by the first program that needs it.
if command -v fc-cache >/dev/null; then
    fc-cache -s -f >/dev/null 2>&1 || true
    echo "fontconfig: $( (find /var/cache/fontconfig -type f 2>/dev/null || true) | wc -l ) cache files"
fi
if command -v gdk-pixbuf-query-loaders >/dev/null; then
    gdk-pixbuf-query-loaders --update-cache >/dev/null 2>&1 || true
fi
for d in /usr/lib/*/gio/modules; do
    [ -d "$d" ] && command -v gio-querymodules >/dev/null && \
        gio-querymodules "$d" >/dev/null 2>&1 || true
done
if command -v update-desktop-database >/dev/null; then
    update-desktop-database /usr/share/applications >/dev/null 2>&1 || true
fi
if command -v update-mime-database >/dev/null; then
    update-mime-database /usr/share/mime >/dev/null 2>&1 || true
fi

# Chromium, like WebKit, draws nothing when its GPU compositor is broken --
# verified on real hardware, where plain `chromium` gave a blank window and
# `chromium --disable-gpu` rendered correctly. Debian's launcher sources
# /etc/chromium.d/*, so this covers every way it can be started.
mkdir -p /etc/chromium.d
cat > /etc/chromium.d/nethos <<'CHROMEFLAGS'
# NETHOS: the GPU compositor cannot be trusted on the hardware this runs on.
# Remove this file to get acceleration back once your driver is known good.
export CHROMIUM_FLAGS="$CHROMIUM_FLAGS --disable-gpu --disable-gpu-compositing"
CHROMEFLAGS

# Strip what a running system never reads.
#
# Debian packages ship documentation, man pages and translations for eighty-odd
# languages, and on a 2.5G system that is several hundred megabytes nobody will
# open. Removed here rather than never installed, because npkg installs whole
# packages -- and doing it as a step makes it visible and reversible instead of
# a silent policy buried in the converter.
#
# Kept: /usr/share/doc/*/copyright, because the licence is the one file in
# there that has to stay.
echo "--- trimming ---"
before=$(du -sm /usr/share 2>/dev/null | cut -f1)

find /usr/share/doc -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while read -r d; do
    find "$d" -type f ! -name copyright -delete 2>/dev/null || true
done
rm -rf /usr/share/man /usr/share/info /usr/share/groff /usr/share/lintian \
       /usr/share/linda /usr/share/bug 2>/dev/null || true

# Locales: keep C and English, drop the rest.
if [ -d /usr/share/locale ]; then
    find /usr/share/locale -mindepth 1 -maxdepth 1 -type d \
        ! -name 'en*' ! -name 'C*' -exec rm -rf {} + 2>/dev/null || true
fi

# Icon themes ship every size from 8px to 512px in several formats. The shell
# uses 32 and 48 and falls back to initials, so the rest is weight.
for theme in /usr/share/icons/*/; do
    [ -d "$theme" ] || continue
    find "$theme" -maxdepth 1 -type d \
        \( -name '8x8' -o -name '12x12' -o -name '96x96' -o -name '128x128' \
           -o -name '256x256' -o -name '512x512' \) -exec rm -rf {} + 2>/dev/null || true
done

after=$(du -sm /usr/share 2>/dev/null | cut -f1)
echo "trimmed /usr/share: ${before:-?}MB -> ${after:-?}MB"

# openssh, minus its postinst. Debian ships the config template in
# /usr/share/openssh and the postinst installs it; without that step sshd exits
# immediately with "/etc/ssh/sshd_config: No such file or directory". Host keys
# are per-machine, so they are generated on first boot rather than baked into
# every image.
if [ -f /usr/share/openssh/sshd_config ] && [ ! -f /etc/ssh/sshd_config ]; then
    mkdir -p /etc/ssh
    cp /usr/share/openssh/sshd_config /etc/ssh/sshd_config
    echo "sshd_config installed"
fi
cat > /etc/systemd/system/nethos-sshd-keys.service <<'SSHKEYS'
[Unit]
Description=Generate ssh host keys on first boot
ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key
Before=ssh.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/ssh-keygen -A

[Install]
WantedBy=multi-user.target
SSHKEYS
mkdir -p /etc/systemd/system/multi-user.target.wants
ln -sf /etc/systemd/system/nethos-sshd-keys.service \
    /etc/systemd/system/multi-user.target.wants/nethos-sshd-keys.service

# File capabilities do not survive the .deb -> .npk conversion, and ping is the
# one that shows: without cap_net_raw it cannot open a raw socket and reports
# "Operation not permitted". Debian's dpkg applies these from package metadata;
# we apply the handful that matter.
if command -v setcap >/dev/null; then
    for pair in "/usr/bin/ping cap_net_raw+ep" \
                "/usr/bin/ping6 cap_net_raw+ep" \
                "/usr/bin/dumpcap cap_net_raw,cap_net_admin+eip"; do
        set -- $pair
        [ -f "$1" ] && setcap "$2" "$1" 2>/dev/null || true
    done
fi

KVER=$(ls /usr/lib/modules 2>/dev/null | head -1)
echo "kernel modules: ${KVER:-none}"
[ -n "$KVER" ] || { echo "FATAL: no kernel installed"; exit 1; }

echo "--- module map ---"
depmod -a "$KVER"
ls /usr/lib/modules/$KVER/modules.dep >/dev/null && echo "modules.dep generated"

# initramfs-tools expects its scripts directories to exist.
mkdir -p /etc/initramfs-tools/hooks
for d in init-top init-premount init-bottom local-top local-premount \
         local-block local-bottom panic; do
    mkdir -p "/etc/initramfs-tools/scripts/$d"
done

# Say explicitly what the initramfs must contain rather than trusting
# autodetection inside a chroot, where /sys belongs to the builder and not to
# the machine this image will boot on.
mkdir -p /etc/initramfs-tools/conf.d
cat > /etc/initramfs-tools/initramfs.conf <<CONF
MODULES=most
BUSYBOX=auto
COMPRESS=zstd
DEVICE=
NFSROOT=auto
RUNSIZE=10%
CONF
cat > /etc/initramfs-tools/modules <<MODS
virtio
virtio_pci
virtio_blk
virtio_scsi
virtio_net
virtio_gpu
virtio_console
ext4
nvme
ahci
sd_mod
usbhid
xhci_pci
MODS

# A kernel built rather than packaged, if one was handed to us.
#
# npkg can only install what Debian ships, and Debian does not ship mainline --
# 7.x is not in trixie at all. nethos-kernel build produces a tarball; this
# unpacks it into the image so the result boots the kernel you built rather
# than the one the archive had. KVER is recomputed afterwards because the
# initramfs below must be built for the kernel that will actually boot.
if [ -f /root/custom-kernel.tgz ]; then
    echo "--- unpacking the supplied kernel ---"

    # Make room before unpacking, not after it fails.
    #
    # Two complete kernels do not fit. A distro module tree is several hundred
    # megabytes and the image is sized for one, so unpacking a second on top of
    # the packaged one fills the root filesystem partway through and leaves a
    # shredded /lib/modules behind a thousand lines of tar errors. The packaged
    # kernel is not worth keeping for the space either: stripped of its modules
    # it cannot mount the root filesystem, so it is not a fallback, and the A/B
    # slots are the real safety net.
    echo "    removing the packaged kernel $KVER to make room"
    rm -rf "/lib/modules/$KVER" "/boot/initrd.img-$KVER" "/boot/vmlinuz-$KVER" \
           "/boot/config-$KVER" "/boot/System.map-$KVER"

    # Refuse to start an unpack that cannot finish. Compressed module trees
    # expand by roughly three; anything less than that free is a build that
    # fails an hour in rather than here, with a message that says so.
    tar_kb=$(( $(stat -c%s /root/custom-kernel.tgz) / 1024 ))
    free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
    need_kb=$(( tar_kb * 3 ))
    echo "    tarball ${tar_kb}K, needs about ${need_kb}K, ${free_kb}K free"
    if [ "$free_kb" -lt "$need_kb" ]; then
        echo "FATAL: not enough room in the image for this kernel."
        echo "  free: ${free_kb}K   needed: about ${need_kb}K"
        echo "  Build a larger disk:  ./scripts/build-x86.sh --size 16G"
        exit 1
    fi

    tar xf /root/custom-kernel.tgz -C / || {
        echo "FATAL: the kernel tarball did not unpack."
        echo "  $(df -Ph / | awk 'NR==2 {print $4" free of "$2}')"
        exit 1
    }
    NEWKVER=$(ls -1 /lib/modules | sort -V | tail -1)
    if [ -n "$NEWKVER" ] && [ -e "/boot/vmlinuz-$NEWKVER" ]; then
        KVER="$NEWKVER"
        echo "    booting $KVER instead of the packaged kernel"
        depmod -a "$KVER"
    else
        echo "FATAL: the tarball unpacked but produced no bootable kernel."
        echo "  /lib/modules holds: $(ls -1 /lib/modules 2>/dev/null | tr '\n' ' ')"
        echo "  /boot holds:        $(ls -1 /boot/vmlinuz-* 2>/dev/null | tr '\n' ' ')"
        echo "  The packaged kernel was removed to make room, so there is"
        echo "  nothing to fall back to. Check the tarball is a targz-pkg."
        exit 1
    fi
fi

# Put /lib/firmware back, because unpacking the kernel just destroyed it.
#
# make targz-pkg writes a bare "lib/" directory entry into the tarball. /lib is
# a symlink to usr/lib, and GNU tar extracting a directory over a symlink
# replaces the symlink with a real directory -- so the moment the kernel is
# unpacked, /usr/lib/firmware is orphaned and /lib/firmware contains nothing
# but modules. The kernel searches /lib/firmware and nowhere else.
#
# Everything followed from this. amdgpu would not bind, so the desktop fell
# back to software rendering and drew glitched windows; rtw89 would not bind,
# so there was no wifi on any machine. Both drivers were present and correct
# the whole time and neither could find a single byte of firmware.
#
# It has to run after the unpack. The first version of this check ran before
# it, saw the symlink still intact, and reported "firmware: /lib/firmware
# present (2174 files)" -- measuring the state immediately before the step
# that broke it.
if [ -d /usr/lib/firmware ] && [ ! -e /lib/firmware ]; then
    ln -s ../usr/lib/firmware /lib/firmware
    echo "firmware: relinked /lib/firmware after the kernel unpack ($(find -L /lib/firmware -type f 2>/dev/null | wc -l) files)"
elif [ -e /lib/firmware ]; then
    echo "firmware: /lib/firmware intact ($(find -L /lib/firmware -type f 2>/dev/null | wc -l) files)"
else
    echo "WARNING: no firmware anywhere -- wifi and AMD graphics will not work"
fi
if [ "$(find -L /lib/firmware -type f 2>/dev/null | wc -l)" -lt 100 ]; then
    echo "FATAL: /lib/firmware exposes almost nothing; wifi and GPU will not work"
    exit 1
fi

echo "--- initramfs ---"
# /boot is the ESP, and it is the small one. Reporting / here was actively
# misleading: it said 16G free while the initrd was failing to fit in 512MB.
echo "    /     $(df -Ph /     | awk 'NR==2 {print $4" free of "$2}')"
echo "    /boot $(df -Ph /boot | awk 'NR==2 {print $4" free of "$2}')  <- the initrd goes here"
echo "    $(find /lib/modules/"$KVER" -name '*.ko*' 2>/dev/null | wc -l) modules"
update-initramfs -c -k "$KVER" || {
    echo "update-initramfs failed; falling back to a bare initramfs"
    mkinitramfs -o "/boot/initrd.img-$KVER" "$KVER" || {
        echo "FATAL: the initramfs could not be built."
        df -Ph / /boot | sed 's/^/  /'
        echo
        echo "  If /boot is the full one, the initrd does not fit in the ESP."
        echo "  An initramfs built with MODULES=most over a large module tree is"
        echo "  many times the size of a distro one -- and the ESP holds a kernel"
        echo "  and an initrd for BOTH A/B slots. Give it more room:"
        echo "      NETHOS_ESP_MB=3072 ./scripts/build-x86.sh"
        echo
        echo "  The lasting fix is fewer modules: this kernel was configured from"
        echo "  a distro config that builds nearly every driver. Basing it on"
        echo "  defconfig plus payload/kernel/nethos.config cuts it by an order"
        echo "  of magnitude and speeds up every boot."
        exit 1
    }
}
ls -la /boot/ | head

# Verify the image can actually reach a disk before calling this a build.
#
# A driver counts if it is a module inside the initramfs OR built into the
# kernel, and the difference is a property of whoever configured that kernel.
# Checking only for the module declared a perfectly good 7.2 build broken
# because its config has CONFIG_VIRTIO_BLK=y and so ships no .ko at all.
if command -v lsinitramfs >/dev/null; then
    INITRD_LIST=$(lsinitramfs "/boot/initrd.img-$KVER" 2>/dev/null || true)
    total=$(printf '%s\n' "$INITRD_LIST" | grep -c "\.ko" || true)
    echo "initramfs: ${total:-0} modules"

    missing=""
    check_driver() {   # $1 module name, $2 config symbol, $3 what it reaches
        # usb_storage the module is usb-storage.ko on disk, so match both.
        alt=$(printf '%s' "$1" | tr '_' '-')
        if printf '%s\n' "$INITRD_LIST" | grep -qE "/($1|$alt)\.ko"; then
            echo "    $1: module in initramfs   ($3)"
        elif grep -q "^$2=y" "/boot/config-$KVER" 2>/dev/null; then
            echo "    $1: built into the kernel  ($3)"
        else
            echo "    $1: MISSING                ($3)"
            missing="$missing $1"
        fi
    }
    check_driver virtio_blk  CONFIG_VIRTIO_BLK  "the disk in a VM"
    check_driver usb_storage CONFIG_USB_STORAGE "a USB stick"
    check_driver nvme        CONFIG_BLK_DEV_NVME "an internal SSD"

    if [ -n "$missing" ]; then
        echo "FATAL: no way to reach the root filesystem --$missing"
        echo "  Neither in the initramfs nor built into the kernel. This image"
        echo "  would boot to an initramfs prompt on the hardware it is for."
        exit 1
    fi
fi

# targz-pkg ships the unstripped vmlinux -- half a gigabyte of ELF that nothing
# boots from. On the ESP, where space is measured against two A/B slots, it is
# the single largest thing there and is pure waste.
if [ -f "/boot/vmlinux-$KVER" ]; then
    echo "removing /boot/vmlinux-$KVER ($(du -h "/boot/vmlinux-$KVER" | cut -f1), not used to boot)"
    rm -f "/boot/vmlinux-$KVER"
fi

echo "--- bootloader ---"
grub-install --target=x86_64-efi --efi-directory=/boot \
             --bootloader-id=NETHOS --removable --no-nvram
cat > /etc/default/grub <<GRUB
GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_TIMEOUT_STYLE=hidden
GRUB_RECORDFAIL_TIMEOUT=0
GRUB_DISTRIBUTOR="NETHOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 systemd.show_status=false vt.global_cursor_default=0 console=tty0 console=ttyS0,115200"
GRUB_CMDLINE_LINUX=""
GRUB_TERMINAL="console serial"
GRUB
grub-mkconfig -o /boot/grub/grub.cfg

# Whatever grub-probe decided, the UUID we formatted the disk with is the
# truth. Correct it and then verify, rather than hoping.
if [ -n "${ROOT_UUID:-}" ]; then
    sed -i "s|root=UUID=[0-9a-fA-F-]*|root=UUID=$ROOT_UUID|g" /boot/grub/grub.cfg
    wrong=$(grep -o "root=UUID=[0-9a-fA-F-]*" /boot/grub/grub.cfg \
            | grep -cv "root=UUID=$ROOT_UUID" || true)
    right=$(grep -c "root=UUID=$ROOT_UUID" /boot/grub/grub.cfg || true)
    echo "grub root=UUID: $right correct, ${wrong:-0} wrong (target $ROOT_UUID)"
    if [ "${right:-0}" -lt 1 ]; then
        echo "FATAL: grub.cfg has no boot entry pointing at the root filesystem"
        exit 1
    fi
fi
grep -c "^menuentry" /boot/grub/grub.cfg | xargs echo "grub menu entries:"

date -u +'%Y-%m-%dT%H:%M:%SZ' > /etc/nethos-release
echo "--- done inside ---"
INSIDE

# The seed is mounted at /mnt/src in the builder, which is not visible inside
# the chroot -- so a supplied kernel has to be carried across before inside.sh
# looks for it.
# Find it by glob, and insist on it if one was supplied.
#
# ISO9660 permits one dot in a filename. Staging the kernel as
# custom-kernel.tar.gz meant hdiutil wrote custom-kerneltar.gz, the test for
# the original name failed, and the build quietly carried on to produce an
# image running Debian's kernel instead of the one that was asked for -- with
# nothing in the log saying so. The name has one dot now; the glob is here so
# that a different tool mangling it differently cannot cost another hour.
CK=$(ls "$SRC"/custom-kernel* 2>/dev/null | head -1 || true)
if [ -n "$CK" ]; then
    echo "supplied kernel: $(basename "$CK")"
    cp "$CK" "$R/root/custom-kernel.tgz"
elif [ "${WANT_CUSTOM_KERNEL:-0}" = 1 ]; then
    echo "FATAL: a kernel tarball was staged but is not on the seed medium."
    echo "  looked in $SRC for custom-kernel*, found:"
    ls -la "$SRC" | sed 's/^/    /'
    exit 1
fi

chmod +x "$R/root/inside.sh"
chroot "$R" env ROOT_UUID="$ROOT_UUID" ESP_UUID="$ESP_UUID" /root/inside.sh
rm -f "$R/root/custom-kernel.tgz" "$R/root/inside.sh"
rm -f "$R/root/inside.sh"

# Size, reported inside a guard that cannot fail.
#
# This block has now broken the build twice, in two different ways, both of
# them pipefail: `du | awk` reports failure because du exits non-zero the
# moment it meets one unreadable path -- 2>/dev/null hides the message, not
# the status -- and `sort | head` reports failure because head closes the pipe
# and sort dies of SIGPIPE. Both printed their output first and then killed a
# build in which every actual step had succeeded.
#
# A diagnostic must never be able to fail the thing it is measuring, so the
# whole block is wrapped rather than each line being argued with individually.
{
    echo "--- size ---"
    total=$(du -sh "$R" 2>/dev/null | cut -f1) || total="?"
    echo "  total: ${total:-?}"
    sizes=$(du -sm "$R"/usr/* "$R"/var/* 2>/dev/null | sort -rn) || sizes=""
    if [ -n "$sizes" ]; then
        printf '%s\n' "$sizes" | awk 'NR<=8 {printf "  %6dMB  %s\n", $1, $2}'
    fi
} || true

echo "--- sanity ---"
ls -l "$R/usr/bin/sudo" "$R/usr/bin/su"
ls "$R/boot" | head
sync
umount -R "$R" || true
# Flush the cache before the power is cut, or the next build finds a dirty
# filesystem and re-downloads the lot.
umount "$WORK" 2>/dev/null || true
sync
echo "=== NETHOS x86 image build finished $(date -u) ==="
poweroff
BOOTSTRAP

cat > "$STAGE/user-data" <<'USERDATA'
#cloud-config
datasource_list: [ NoCloud, None ]
# The builder is a throwaway VM on loopback, but it still gets a real login so
# a stalled build can be inspected instead of guessed at:
#   ssh -p 2223 builder@127.0.0.1   (password: builder)
users:
  - name: builder
    lock_passwd: false
    plain_text_passwd: builder
    sudo: ["ALL=(ALL:ALL) NOPASSWD:ALL"]
    shell: /bin/bash
ssh_pwauth: true
chpasswd:
  expire: false
  list: |
    root:builder
runcmd:
  - [ mkdir, -p, /mnt/seed ]
  - [ mount, -o, ro, /dev/sr0, /mnt/seed ]
  - [ bash, /mnt/seed/build.sh ]
USERDATA
printf 'instance-id: nethos-x86\nlocal-hostname: nethos-builder\n' > "$STAGE/meta-data"

rm -f "$SEED"
# The cloud-init seed, built with whatever this machine has. hdiutil is macOS
# only, and this script is most useful on a Linux x86 box where it can use KVM
# -- which is exactly where hdiutil does not exist.
if command -v hdiutil >/dev/null 2>&1; then
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name CIDATA -o "$SEED" "$STAGE"
elif command -v xorriso >/dev/null 2>&1; then
    xorriso -as mkisofs -quiet -output "$SEED" -volid CIDATA -joliet -rational-rock "$STAGE"
elif command -v genisoimage >/dev/null 2>&1; then
    genisoimage -quiet -output "$SEED" -volid CIDATA -joliet -rock "$STAGE"
elif command -v mkisofs >/dev/null 2>&1; then
    mkisofs -quiet -output "$SEED" -volid CIDATA -joliet -rock "$STAGE"
else
    die "No tool to build the cloud-init seed ISO.
  Arch/CachyOS:   sudo pacman -S libisoburn      (xorriso)
  Debian/Ubuntu:  sudo apt install xorriso
  macOS:          hdiutil is built in"
fi
[ -s "$SEED" ] || die "seed ISO was not created at $SEED"

# --------------------------------------------------------------------------
say "Building (accel=$ACCEL). Downloads and installs a full base system."

qemu-system-x86_64 \
    -name nethos-x86-builder \
    -machine q35,accel="$ACCEL" \
    -cpu "$CPU" -smp "$VM_CPUS" -m "$VM_MEM" \
    -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
    -drive if=pflash,format=raw,file="$FW_VARS" \
    -drive file="$BUILDER_WORK",if=virtio,format=qcow2 \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$CACHE",if=virtio,format=qcow2 \
    -drive file="$SEED",if=none,id=seed,format=raw,media=cdrom,readonly=on \
    -device ide-cd,drive=seed \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0,hostfwd=tcp::2223-:22 \
    -device virtio-rng-pci \
    -nographic

rm -f "$BUILDER_WORK"

# QEMU exiting means the VM powered off, not that the build worked -- a failed
# build powers off too, and this script cheerfully reported "Built:" over the
# top of it. The guest prints a marker on the way out; require it.
if ! grep -aq "=== NETHOS x86 image build finished" "$LOG"; then
    echo
    echo "BUILD FAILED. The builder powered off without finishing." >&2
    grep -aE "FATAL|WARNING|Failed to run module" "$LOG" | tail -5 >&2
    echo "Full log: $LOG" >&2
    exit 1
fi

say "Built: $DISK"
echo
echo "  Boot it:  scripts/run.sh --arch x86_64"
echo "  Log in:   $USERNAME / nethos   (root also nethos)"
echo
