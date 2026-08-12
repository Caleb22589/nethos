#!/bin/bash
# Build a bootable NETHOS disk image from converted Debian packages.
#
#     scripts/build-image.sh
#     scripts/run.sh --arch aarch64
#
# The work happens inside a throwaway Debian arm64 VM under HVF, because it has
# to happen as root: setuid bits survive a tarball but file *ownership* does
# not, and a sudo owned by anyone but root refuses to run. macOS also cannot
# mount ext4, so there is nowhere to install to from the host.
#
#     Debian arm64 (builder, HVF, root)
#          ├── partitions /dev/vdb: ESP + ext4 root
#          ├── npkg-bootstrap: resolve, download, convert, install
#          ├── chroot: initramfs, machine-id, fstab, default target
#          └── GRUB for arm64-efi, then powers off
#
# Options:
#   --clean          start from an empty disk
#   --size 20G       disk size (default 20G)
#   --user NAME      the account to create (default neth)
#   --sets "a b"     package sets (default "base system kernel desktop")
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DISK="$BUILD/nethos-arm.qcow2"
BUILDER="$BUILD/debian-arm64-builder.qcow2"
BUILDER_WORK="$BUILD/debian-arm64-work.qcow2"
# The package cache, and the one disk here that deliberately outlives a build.
# The builder overlay is thrown away and recreated every run, so anything on it
# -- including several hundred megabytes of downloaded .deb -- was being fetched
# again from scratch every single time. This holds them across builds; a rebuild
# with no version changes downloads nothing at all.
CACHE="$BUILD/nethos-pkgcache.qcow2"
SEED="$BUILD/seed-image.iso"
BUILDER_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"

DISK_SIZE="20G"
USERNAME="neth"
SETS="base system kernel desktop"
CLEAN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1; shift ;;
        --size) DISK_SIZE="${2:?}"; shift 2 ;;
        --user) USERNAME="${2:?}"; shift 2 ;;
        --sets) SETS="${2:?}"; shift 2 ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# Always keep a log next to the images, so there is somewhere to look without
# having to remember how the build was launched.
mkdir -p "$BUILD"
LOG="$BUILD/build-image.log"
exec > >(tee "$LOG") 2>&1

say() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v qemu-system-aarch64 >/dev/null || die "brew install qemu"
FW_CODE=/opt/homebrew/share/qemu/edk2-aarch64-code.fd
[ -f "$FW_CODE" ] || FW_CODE=/usr/local/share/qemu/edk2-aarch64-code.fd
[ -f "$FW_CODE" ] || die "edk2-aarch64-code.fd not found"

ACCEL=tcg; CPU=max
if [ "$(uname -m)" = "arm64" ] && [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
    ACCEL=hvf; CPU=host
fi

mkdir -p "$BUILD"
[ "$CLEAN" -eq 1 ] && rm -f "$DISK"

if [ ! -f "$BUILDER" ]; then
    say "Downloading the Debian arm64 builder (~430 MB, once)"
    curl -fL --retry 3 -o "$BUILDER" "$BUILDER_URL"
fi

say "Creating the target disk ($DISK_SIZE)"
rm -f "$DISK"
qemu-img create -f qcow2 "$DISK" "$DISK_SIZE" >/dev/null

rm -f "$BUILDER_WORK"
qemu-img create -f qcow2 -F qcow2 -b "$BUILDER" "$BUILDER_WORK" >/dev/null
qemu-img resize "$BUILDER_WORK" 16G >/dev/null

# Created once and then kept. Deleting it only costs a re-download.
if [ ! -f "$CACHE" ]; then
    say "Creating the package cache (kept between builds; rm $CACHE to reset)."
    qemu-img create -f qcow2 "$CACHE" 24G >/dev/null
fi

FW_VARS="$BUILD/edk2-arm-vars-build.fd"
rm -f "$FW_VARS"
dd if=/dev/zero of="$FW_VARS" bs=1m count=64 2>/dev/null

# --------------------------------------------------------------------------
say "Staging npkg and the build plan"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nethos-image.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/pkg"
cp "$ROOT"/pkg/*.py "$STAGE/pkg/"
# The shell rides along: npkg_bootstrap looks for it beside the pkg directory.
cp -R "$ROOT/payload" "$STAGE/payload"

cat > "$STAGE/build.sh" <<BOOTSTRAP
#!/bin/bash
# Runs as root inside the builder. /dev/vdb is the target disk.
set -euo pipefail
exec > >(tee -a /var/log/nethos-image.log) 2>&1
echo "=== NETHOS image build starting \$(date -u) ==="

USERNAME="$USERNAME"
SETS="$SETS"
BOOTSTRAP
cat >> "$STAGE/build.sh" <<'BOOTSTRAP'

export DEBIAN_FRONTEND=noninteractive

# A failed build must still power the VM down. Without this the builder sits at
# a login prompt holding a write lock on the target and cache disks, and the
# next build dies instantly with 'Failed to get "write" lock' -- which looks
# like a corrupt image rather than a leftover process.
trap 'st=$?; echo "=== NETHOS image build FAILED (status $st) ==="; sync; poweroff -f' ERR

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
parted -s "$TARGET" mkpart ESP fat32 1MiB 513MiB
parted -s "$TARGET" set 1 esp on
parted -s "$TARGET" mkpart root ext4 513MiB 100%
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
    --arch arm64 --user "$USERNAME" --work "$WORK" --keep

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
echo "--- inside the NETHOS root ---"

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
    # /etc/ca-certificates.conf lists what to trust and is written by the
    # ca-certificates postinst, not shipped in the .deb. Without it the command
    # succeeds and produces an empty trust store, and every https client fails
    # while the certificates sit unused in /usr/share/ca-certificates.
    if [ -d /usr/share/ca-certificates ] && [ ! -s /etc/ca-certificates.conf ]; then
        ( cd /usr/share/ca-certificates && find . -name '*.crt' | sed 's|^\./||' | sort ) \
            > /etc/ca-certificates.conf
        echo "ca-certificates.conf: $(wc -l < /etc/ca-certificates.conf) certificates listed"
    fi
    update-ca-certificates --fresh >/dev/null 2>&1 || true
    # Count what Debian actually writes: the concatenated bundle every TLS
    # library reads, plus the c_rehash symlinks. Counting *.pem reported zero
    # on a perfectly good trust store, because Debian does not put any there.
    #
    # find, not `ls glob`: with pipefail set, ls exits 2 when a glob matches
    # nothing and the pipeline inherits that rather than wc's zero, which
    # aborted the whole build on an empty directory.
    certs=$( (find /etc/ssl/certs -maxdepth 1 -name '*.0' 2>/dev/null || true) | wc -l )
    bundle=/etc/ssl/certs/ca-certificates.crt
    if [ -s "$bundle" ]; then
        echo "ca-certificates: $certs hashed, bundle $(wc -c < "$bundle") bytes"
    else
        echo "WARNING: no CA bundle at $bundle; https will fail"
    fi
    # Written as if/fi rather than `[ test ] && echo`: under set -e a trailing
    # && list that evaluates false is a non-zero status for the whole block,
    # so the success case would abort the build.
    if [ "$certs" -eq 0 ]; then
        echo "WARNING: no CA certificates; https will fail"
    fi
fi

# The rest of the caches Debian builds from postinsts and dpkg triggers. None
# of these are fatal on their own, which is what makes them worth doing here:
# each one is instead paid for at runtime by the first program that needs it.
# An empty /var/cache/fontconfig means every GUI application rebuilds the font
# cache itself on first launch.
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
# Debian's kernel package generates modules.dep in its postinst; we do not run
# postinsts, so without this initramfs-tools has no dependency map, silently
# builds an initramfs with no virtio_blk in it, and the kernel then cannot find
# the root filesystem at all: "ALERT! UUID=... does not exist".
depmod -a "$KVER"
ls /usr/lib/modules/$KVER/modules.dep >/dev/null && echo "modules.dep generated"

# initramfs-tools expects its scripts directories to exist. They ship as empty
# directories in the package, and empty directories are exactly what a
# conversion is most likely to lose. When they are missing mkinitramfs fails a
# cd early on -- printing a line that reads like a warning -- and never gets as
# far as honouring the module list, so the initramfs comes out without a disk
# driver. That one "harmless" message cost three build cycles.
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

echo "--- initramfs ---"
# Without this the kernel cannot reach the root filesystem: Debian builds
# virtio_blk as a module, so something has to load it before the pivot.
update-initramfs -c -k "$KVER" || {
    echo "update-initramfs failed; falling back to a bare initramfs"
    mkinitramfs -o "/boot/initrd.img-$KVER" "$KVER"
}
ls -la /boot/ | head

# Verify the initramfs can actually reach a disk before calling this a build.
# Finding out at boot costs a full cycle; finding out here costs a grep.
if command -v lsinitramfs >/dev/null; then
    # grep -c, not grep -q, and the count captured before it is tested.
    #
    # This script runs under `set -o pipefail`, and `grep -q` exits the moment
    # it finds a match. That closes the pipe, lsinitramfs dies of SIGPIPE, and
    # pipefail turns the whole pipeline non-zero -- so the test failed
    # *because* the match succeeded. It reported a missing driver on an
    # initramfs that had it, and cost two rebuilds chasing a bug that was in
    # the check. grep -c consumes all of its input, so nothing gets a SIGPIPE.
    found=$(lsinitramfs "/boot/initrd.img-$KVER" 2>/dev/null \
            | grep -c "virtio_blk" || true)
    total=$(lsinitramfs "/boot/initrd.img-$KVER" 2>/dev/null \
            | grep -c "\.ko" || true)
    echo "initramfs: ${total:-0} modules, virtio_blk x${found:-0}"
    if [ "${found:-0}" -lt 1 ]; then
        echo "FATAL: initramfs has no virtio_blk -- it would not find the root"
        exit 1
    fi
fi

echo "--- bootloader ---"
grub-install --target=arm64-efi --efi-directory=/boot \
             --bootloader-id=NETHOS --removable --no-nvram
cat > /etc/default/grub <<GRUB
GRUB_DEFAULT=0
GRUB_TIMEOUT=1
GRUB_DISTRIBUTOR="NETHOS"
GRUB_CMDLINE_LINUX_DEFAULT="console=tty0 console=ttyAMA0,115200"
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

chmod +x "$R/root/inside.sh"
chroot "$R" env ROOT_UUID="$ROOT_UUID" ESP_UUID="$ESP_UUID" /root/inside.sh
rm -f "$R/root/inside.sh"

echo "--- sanity ---"
ls -l "$R/usr/bin/sudo" "$R/usr/bin/su"
ls "$R/boot" | head
sync
umount -R "$R" || true
# Flush the cache before the power is cut, or the next build finds a dirty
# filesystem and re-downloads the lot.
umount "$WORK" 2>/dev/null || true
sync
echo "=== NETHOS image build finished $(date -u) ==="
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
printf 'instance-id: nethos-image\nlocal-hostname: nethos-builder\n' > "$STAGE/meta-data"

rm -f "$SEED"
# hdiutil is macOS only; use whatever this machine has.
if command -v hdiutil >/dev/null 2>&1; then
    hdiutil makehybrid -quiet -iso -joliet -default-volume-name CIDATA -o "$SEED" "$STAGE"
elif command -v xorriso >/dev/null 2>&1; then
    xorriso -as mkisofs -quiet -output "$SEED" -volid CIDATA -joliet -rational-rock "$STAGE"
elif command -v genisoimage >/dev/null 2>&1; then
    genisoimage -quiet -output "$SEED" -volid CIDATA -joliet -rock "$STAGE"
elif command -v mkisofs >/dev/null 2>&1; then
    mkisofs -quiet -output "$SEED" -volid CIDATA -joliet -rock "$STAGE"
else
    die "No tool to build the cloud-init seed ISO (hdiutil/xorriso/genisoimage)"
fi
[ -s "$SEED" ] || die "seed ISO was not created at $SEED"

# --------------------------------------------------------------------------
say "Building (accel=$ACCEL). Downloads and installs a full base system."

qemu-system-aarch64 \
    -name nethos-image-builder \
    -machine virt,accel="$ACCEL",highmem=on \
    -cpu "$CPU" -smp 4 -m 4096 \
    -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
    -drive if=pflash,format=raw,file="$FW_VARS" \
    -drive file="$BUILDER_WORK",if=virtio,format=qcow2 \
    -drive file="$DISK",if=virtio,format=qcow2 \
    -drive file="$CACHE",if=virtio,format=qcow2 \
    -drive file="$SEED",if=none,id=seed,format=raw,media=cdrom,readonly=on \
    -device virtio-scsi-pci -device scsi-cd,drive=seed \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0,hostfwd=tcp::2223-:22 \
    -device virtio-rng-pci \
    -nographic

rm -f "$BUILDER_WORK"

# QEMU exiting means the VM powered off, not that the build worked -- a failed
# build powers off too, and this script cheerfully reported "Built:" over the
# top of it. The guest prints a marker on the way out; require it.
if ! grep -aq "=== NETHOS image build finished" "$LOG"; then
    echo
    echo "BUILD FAILED. The builder powered off without finishing." >&2
    grep -aE "FATAL|WARNING|Failed to run module" "$LOG" | tail -5 >&2
    echo "Full log: $LOG" >&2
    exit 1
fi

say "Built: $DISK"
echo
echo "  Boot it:  scripts/run.sh --arch aarch64"
echo "  Log in:   $USERNAME / nethos   (root also nethos)"
echo
