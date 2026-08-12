#!/bin/bash
# Run the NETHOS VM — on x86_64 or on ARM.
#
#   scripts/run.sh                 pick the best image for this Mac
#   scripts/run.sh --arch aarch64  force the ARM image
#   scripts/run.sh --arch x86_64   force the x86 image
#   scripts/run.sh --console       serial only, no GUI window
#   scripts/run.sh --reset-uefi    rebuild the UEFI variable store; use
#                                  this if the VM lands on a Shell> prompt
#
# Why two architectures:
#
#   On Apple Silicon the x86_64 image runs under TCG, which emulates every
#   instruction in software. It works, but it is slow enough to be unpleasant.
#   The aarch64 image runs under HVF — Apple's own hypervisor — executing ARM
#   instructions natively. Same NETHOS, an order of magnitude faster.
#
#   The x86 image is still the one that matches real PC hardware, so keep it
#   for testing what you will actually install. Use ARM for daily work on the
#   Mac.
#
# Build the ARM image with scripts/build-arm.sh, the x86 one with build-x86.sh
# (npkg/Debian) or build.sh (Arch cloud).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"

ARCH=""
CONSOLE=0
RESET_UEFI=0
MEM="${MEM:-6144}"
CPUS="${CPUS:-4}"

while [ $# -gt 0 ]; do
    case "$1" in
        --arch) ARCH="${2:?--arch needs x86_64 or aarch64}"; shift 2 ;;
        --console) CONSOLE=1; shift ;;
        --reset-uefi) RESET_UEFI=1; shift ;;
        --no-gl) NO_GL=1; shift ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

X86_DISK="$BUILD/nethos.qcow2"
X86_NPKG_DISK="$BUILD/nethos-x86.qcow2"
ARM_DISK="$BUILD/nethos-arm.qcow2"
SEED="$BUILD/seed.iso"

# Pick the fast one when it exists and this Mac can run it natively.
if [ -z "$ARCH" ]; then
    if [ "$(uname -m)" = "arm64" ] && [ -f "$ARM_DISK" ]; then
        ARCH=aarch64
    else
        ARCH=x86_64
    fi
fi

die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

# Pick a display backend this QEMU actually has, rather than assuming the Mac's.
# Arch splits QEMU up: qemu-base is headless and offers only "none", so hard-
# coding cocoa fails with "Parameter 'type' does not accept value 'cocoa'" and
# hard-coding gtk would fail the same way.
QEMU_BIN="qemu-system-${ARCH}"
AVAIL=$("$QEMU_BIN" -display help 2>/dev/null | tr -d ' ')
pick_display() {
    for d in "$@"; do
        printf '%s\n' "$AVAIL" | grep -qx "$d" && { echo "$d"; return; }
    done
}
case "$(uname -s)" in
    Darwin) DISP=$(pick_display cocoa sdl gtk) ;;
    *)      DISP=$(pick_display gtk sdl) ;;
esac

# Hardware GL for the guest, when every link in the chain is present: a GL
# capable virtio device in this QEMU, virglrenderer on the host, and a display
# backend that can hand over a GL context. This is the single biggest thing
# available to NETHOS performance -- without it sway composites and WebKit
# renders every page through llvmpipe on the CPU.
GL=0
if [ "${NO_GL:-0}" != "1" ] && [ "$CONSOLE" -eq 0 ]; then
    if "$QEMU_BIN" -device help 2>/dev/null | grep -q "virtio-vga-gl\|virtio-gpu-gl-pci" \
       && ls /usr/lib*/libvirglrenderer.so* /usr/lib/*/libvirglrenderer.so* >/dev/null 2>&1; then
        GL=1
    fi
fi

if [ "$CONSOLE" -eq 1 ]; then
    DISPLAY_ARGS=(-display none)
elif [ -z "$DISP" ]; then
    die "This QEMU has no graphical display backend (only: $(printf '%s' "$AVAIL" | tr '\n' ' ')).
  Arch/CachyOS:   sudo pacman -S qemu-desktop     (qemu-base is headless)
  Debian/Ubuntu:  sudo apt install qemu-system-gui
  Or run headless on the serial console:  $0 --console"
elif [ "$DISP" = "cocoa" ]; then
    DISPLAY_ARGS=(-display cocoa,show-cursor=on)
elif [ "$GL" -eq 1 ]; then
    DISPLAY_ARGS=(-display "$DISP,gl=on")
else
    DISPLAY_ARGS=(-display "$DISP")
fi

# The video device has to match: virtio-vga-gl is the one that carries GL
# through to the host, and pairing gl=on with a plain virtio-vga gets you a
# window with no acceleration and no complaint about it.
if [ "$GL" -eq 1 ]; then
    X86_VIDEO=(-device virtio-vga-gl,xres=1600,yres=1000)
    ARM_VIDEO=(-device virtio-gpu-gl-pci,xres=1600,yres=1000)
    say "GPU: virgl enabled (hardware GL in the guest)"
else
    X86_VIDEO=(-device virtio-vga,xres=1440,yres=900)
    ARM_VIDEO=(-device virtio-gpu-pci,xres=1600,yres=1000)
fi

NET=(-device virtio-net-pci,netdev=net0
     -netdev user,id=net0,hostfwd=tcp::2222-:22)

# ---------------------------------------------------------------- aarch64 --
if [ "$ARCH" = "aarch64" ]; then
    [ -f "$ARM_DISK" ] || die "no ARM image yet: $ARM_DISK
Build it with:  scripts/build-arm.sh"

    FW_CODE=""
    for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
             /usr/local/share/qemu/edk2-aarch64-code.fd \
             /usr/share/qemu/edk2-aarch64-code.fd \
             /usr/share/AAVMF/AAVMF_CODE.fd \
             /usr/share/edk2/aarch64/QEMU_EFI.fd \
             /usr/share/edk2-armvirt/aarch64/QEMU_EFI.fd; do
        [ -f "$c" ] && FW_CODE="$c" && break
    done
    [ -n "$FW_CODE" ] || die "edk2-aarch64-code.fd not found (brew install qemu)"

    # UEFI keeps its boot entries in a writable variable store, per VM. Copy QEMU's
    # template rather than inventing 64MB of zeros: a zeroed store leaves the
    # firmware with no boot entries, and it drops to the EFI shell instead of
    # booting. It also gets corrupted if a VM is killed mid-write, with the
    # same symptom, so --reset-uefi puts it back.
    FW_VARS="$BUILD/edk2-arm-vars.fd"
    FW_TEMPLATE="$(dirname "$FW_CODE")/edk2-arm-vars.fd"
    if [ "${RESET_UEFI:-0}" = "1" ]; then
        rm -f "$FW_VARS"
    fi
    if [ ! -f "$FW_VARS" ]; then
        say "Creating the UEFI variable store"
        if [ -f "$FW_TEMPLATE" ]; then
            cp "$FW_TEMPLATE" "$FW_VARS"
        else
            dd if=/dev/zero of="$FW_VARS" bs=1m count=64 2>/dev/null
        fi
    fi

    ACCEL=tcg
    CPU=max
    if [ "$(uname -m)" = "arm64" ] && [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
        ACCEL=hvf
        CPU=host          # native ARM execution, no translation
    fi
    say "aarch64 · accel=$ACCEL · ${CPUS} cpus · ${MEM}MB"

    exec qemu-system-aarch64 \
        -name NETHOS \
        -machine virt,accel="$ACCEL",highmem=on \
        -cpu "$CPU" \
        -smp "$CPUS" \
        -m "$MEM" \
        -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
        -drive if=pflash,format=raw,file="$FW_VARS" \
        -drive file="$ARM_DISK",if=virtio,format=qcow2,cache=writeback,discard=unmap \
        "${ARM_VIDEO[@]}" \
        -device qemu-xhci -device usb-kbd -device usb-tablet \
        -device virtio-rng-pci \
        "${NET[@]}" \
        -serial mon:stdio \
        "${DISPLAY_ARGS[@]}"
fi

# ----------------------------------------------------------------- x86_64 --

if [ "$(uname -m)" = "arm64" ]; then
    say "x86_64 under TCG emulation on an ARM Mac — this will be slow."
    say "For speed:  scripts/build-arm.sh && scripts/run.sh --arch aarch64"
fi

# npkg/Debian image: UEFI-booted, no seed ISO needed.
if [ -f "$X86_NPKG_DISK" ]; then
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
        found=$(find /usr/share -maxdepth 4 -iname 'OVMF_CODE*.fd' 2>/dev/null | head -5)
        [ -n "$found" ] && die "UEFI firmware is installed but at an unexpected path:
$found
Add it to the search list in $0"
        die "No UEFI firmware (OVMF_CODE.fd).
  Arch/CachyOS:   sudo pacman -S edk2-ovmf
  Debian/Ubuntu:  sudo apt install ovmf
  macOS:          brew install qemu"
    fi

    FW_VARS="$BUILD/edk2-x86-vars.fd"
    # The variable store must match the code file. A 4MB OVMF_CODE.4m.fd with a
    # 2MB VARS does not error -- it boots to a UEFI shell and sits there.
    case "$FW_CODE" in
        *OVMF_CODE.4m.fd) FW_TEMPLATE="${FW_CODE%OVMF_CODE.4m.fd}OVMF_VARS.4m.fd" ;;
        *OVMF_CODE_4M.fd) FW_TEMPLATE="${FW_CODE%OVMF_CODE_4M.fd}OVMF_VARS_4M.fd" ;;
        *OVMF_CODE.fd)    FW_TEMPLATE="${FW_CODE%OVMF_CODE.fd}OVMF_VARS.fd" ;;
        *)                FW_TEMPLATE="$(dirname "$FW_CODE")/edk2-i386-vars.fd" ;;
    esac
    if [ "${RESET_UEFI:-0}" = "1" ]; then
        rm -f "$FW_VARS"
    fi
    if [ ! -f "$FW_VARS" ]; then
        say "Creating the UEFI variable store"
        if [ -f "$FW_TEMPLATE" ]; then
            cp "$FW_TEMPLATE" "$FW_VARS"
        else
            # x86_64 pflash has an 8 MB combined limit; match the code file size.
            CODE_SIZE=$(stat -f%z "$FW_CODE" 2>/dev/null || stat -c%s "$FW_CODE")
            dd if=/dev/zero of="$FW_VARS" bs=1 count="$CODE_SIZE" 2>/dev/null
        fi
    fi

    ACCEL="tcg,thread=multi"
    CPU=Nehalem
    if [ "$(uname -s)" = "Linux" ] && [ -w /dev/kvm ]; then
        ACCEL=kvm; CPU=host
    elif [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "x86_64" ] && \
         [ "$(sysctl -n kern.hv_support 2>/dev/null)" = "1" ]; then
        ACCEL=hvf; CPU=host
    fi
    say "x86_64 (npkg) · accel=$ACCEL · ${CPUS} cpus · ${MEM}MB"

    exec qemu-system-x86_64 \
        -name NETHOS \
        -machine q35,accel="$ACCEL" \
        -cpu "$CPU" \
        -smp "$CPUS" \
        -m "$MEM" \
        -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
        -drive if=pflash,format=raw,file="$FW_VARS" \
        -drive file="$X86_NPKG_DISK",if=virtio,format=qcow2,cache=writeback,discard=unmap \
        "${X86_VIDEO[@]}" \
        -device virtio-rng-pci \
        -usb -device usb-tablet -device usb-kbd \
        "${NET[@]}" \
        -serial mon:stdio \
        "${DISPLAY_ARGS[@]}" \
        -boot order=c
fi

# Arch cloud image: legacy path, requires seed ISO.
[ -f "$X86_DISK" ] || die "no x86 image. Run scripts/build-x86.sh or scripts/build.sh first."
[ -f "$SEED" ] || die "no seed ISO. Run scripts/build.sh first."

# CPU model: Nehalem (x86-64-v2 — SSE4.2 + POPCNT, no AVX). `-cpu max` also
# works, but under TCG it advertises AVX/AVX2 in CPUID while OSXSAVE is never
# enabled; a conservative, cleanly emulated model is the safer default.
exec qemu-system-x86_64 \
    -name NETHOS \
    -machine q35 \
    -cpu Nehalem \
    -accel tcg,thread=multi \
    -smp "$CPUS" \
    -m "$MEM" \
    -drive file="$X86_DISK",if=virtio,format=qcow2,cache=writeback,discard=unmap \
    -drive file="$SEED",if=none,id=seed,format=raw,media=cdrom,readonly=on \
    -device ide-cd,drive=seed \
    "${X86_VIDEO[@]}" \
    -device virtio-rng-pci \
    -usb -device usb-tablet -device usb-kbd \
    "${NET[@]}" \
    -serial mon:stdio \
    "${DISPLAY_ARGS[@]}" \
    -boot order=c
