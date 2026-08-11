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
# Build the ARM image with scripts/build-arm.sh, the x86 one with build.sh.
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
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

X86_DISK="$BUILD/nethos.qcow2"
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

DISPLAY_ARGS=(-display cocoa,show-cursor=on)
[ "$CONSOLE" -eq 1 ] && DISPLAY_ARGS=(-display none)

NET=(-device virtio-net-pci,netdev=net0
     -netdev user,id=net0,hostfwd=tcp::2222-:22)

# ---------------------------------------------------------------- aarch64 --
if [ "$ARCH" = "aarch64" ]; then
    [ -f "$ARM_DISK" ] || die "no ARM image yet: $ARM_DISK
Build it with:  scripts/build-arm.sh"

    FW_CODE=""
    for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
             /usr/local/share/qemu/edk2-aarch64-code.fd \
             /usr/share/qemu/edk2-aarch64-code.fd; do
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
        -device virtio-gpu-pci,xres=1600,yres=1000 \
        -device qemu-xhci -device usb-kbd -device usb-tablet \
        -device virtio-rng-pci \
        "${NET[@]}" \
        -serial mon:stdio \
        "${DISPLAY_ARGS[@]}"
fi

# ----------------------------------------------------------------- x86_64 --
[ -f "$X86_DISK" ] || die "no x86 image. Run scripts/build.sh first."
[ -f "$SEED" ] || die "no seed ISO. Run scripts/build.sh first."

if [ "$(uname -m)" = "arm64" ]; then
    say "x86_64 under TCG emulation on an ARM Mac — this will be slow."
    say "For speed:  scripts/build-arm.sh && scripts/run.sh --arch aarch64"
fi

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
    -device virtio-vga,xres=1440,yres=900 \
    -device virtio-rng-pci \
    -usb -device usb-tablet -device usb-kbd \
    "${NET[@]}" \
    -serial mon:stdio \
    "${DISPLAY_ARGS[@]}" \
    -boot order=c
