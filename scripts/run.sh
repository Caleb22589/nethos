#!/bin/bash
# Run the NETHOS VM.
#
# This host is Apple Silicon and the guest is x86_64, so QEMU is doing full
# TCG emulation — there is no hardware acceleration available for this pairing.
# Expect it to be slow, especially during the first-boot package install.
#
# CPU model: Nehalem (x86-64-v2 — SSE4.2 + POPCNT, no AVX). `-cpu max` also
# works, but under TCG it advertises AVX/AVX2 in CPUID while OSXSAVE is never
# enabled; that inconsistency is odd enough that a conservative, cleanly
# emulated model is the safer default. Arch and Chromium both run fine on v2.
#
#   scripts/run.sh              GUI window + serial log in this terminal
#   scripts/run.sh --console    no GUI, serial console only (watch the install)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
DISK="$BUILD/nethos.qcow2"
SEED="$BUILD/seed.iso"

MEM="${MEM:-6144}"
CPUS="${CPUS:-4}"

[ -f "$DISK" ] || { echo "No disk. Run scripts/build.sh first." >&2; exit 1; }
[ -f "$SEED" ] || { echo "No seed ISO. Run scripts/build.sh first." >&2; exit 1; }

DISPLAY_ARGS=(-display cocoa,show-cursor=on)
if [ "${1:-}" = "--console" ]; then
    DISPLAY_ARGS=(-display none)
fi

exec qemu-system-x86_64 \
    -name NETHOS \
    -machine q35 \
    -cpu Nehalem \
    -accel tcg,thread=multi \
    -smp "$CPUS" \
    -m "$MEM" \
    -drive file="$DISK",if=virtio,format=qcow2,cache=writeback,discard=unmap \
    -drive file="$SEED",if=none,id=seed,format=raw,media=cdrom,readonly=on \
    -device ide-cd,drive=seed \
    -device virtio-vga,xres=1440,yres=900 \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0,hostfwd=tcp::2222-:22 \
    -device virtio-rng-pci \
    -usb -device usb-tablet -device usb-kbd \
    -serial mon:stdio \
    "${DISPLAY_ARGS[@]}" \
    -boot order=c
