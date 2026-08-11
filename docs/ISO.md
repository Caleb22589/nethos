# Building a NETHOS ISO and putting it on real hardware

The VM image (`build/nethos.qcow2`) is a virtual disk, not something you can
flash. For real hardware you want the **live ISO** — a hybrid image that boots
on both UEFI and legacy BIOS machines, runs NETHOS entirely from the USB stick,
and can install itself to an internal disk.

## Building it

The ISO is built with `archiso`, which only runs on Arch Linux x86_64. The
NETHOS VM *is* an Arch x86_64 machine, so it can build its own installer:

```bash
# inside the NETHOS VM (or any Arch box)
sudo pacman -S --needed archiso rsync
cd ~/.local/share/nethos/repo && git pull
sudo ./iso/build-iso.sh
```

The ISO lands in `out/`. Options:

```
--out DIR         where to write it        (default ./out)
--work DIR        scratch space            (default /var/tmp/nethos-iso)
--compress zstd   fast, slightly larger    (default)
--compress xz     smaller, far slower
```

Building inside the emulated VM is slow — it pacstraps ~750 MB of packages and
then compresses a multi-gigabyte filesystem on an emulated CPU. On real Arch
hardware the same script takes a few minutes.

### How the profile works

`iso/build-iso.sh` copies Arch's stock `releng` profile and patches it, rather
than vendoring a copy here. That matters: the bootloader configuration is the
part most likely to break across archiso versions, so it always comes from the
archiso you actually have installed.

On top of releng it adds:

- `iso/packages.nethos` — the NETHOS package set, appended to `packages.x86_64`
- the entire `payload/` tree at `/usr/share/nethos-payload`
- `nethos-live-setup.service`, which applies NETHOS on first boot with
  `install-nethos.sh --no-packages` (seconds, since the packages are already in
  the image)
- zstd instead of xz for squashfs

## Flashing it

**macOS.** Find the disk *carefully* — `diskutil list` before and after
plugging the stick in, and be certain which one it is. Writing to the wrong
device destroys it.

```bash
diskutil list
diskutil unmountDisk /dev/diskN
sudo dd if=nethos-*.iso of=/dev/rdiskN bs=4m status=progress
diskutil eject /dev/diskN
```

**Linux.**

```bash
lsblk
sudo dd if=nethos-*.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

**Windows.** Use [Rufus](https://rufus.ie) or [balenaEtcher](https://etcher.balena.io)
in DD/image mode.

The ISO is isohybrid, so it needs no special preparation — a raw byte copy is
correct.

## Booting it

Boot the PC from the USB stick (usually F12, F11, ESC or DEL at power-on for
the boot menu). Secure Boot must be **off** — the ISO is not signed with a key
your firmware trusts.

You get the NETHOS desktop running from the stick. Nothing on the machine's
disks is touched. Changes you make live in RAM and disappear on reboot.

## Installing to the internal disk

From a terminal in the live session (`Super`+`Return`):

```bash
nethos-install --list                    # what disks are here
sudo nethos-install --target /dev/sda -n # dry run: prints the plan, changes nothing
sudo nethos-install --target /dev/sda    # the real thing
```

**This erases the target disk completely** — partition table, filesystems,
other operating systems, all data, with no undo. Run the dry run first and read
the summary. The installer refuses to touch the live USB, a disk with mounted
partitions, or anything that is not a whole disk, and it makes you type
`ERASE /dev/sdX` before it does anything.

What it does:

1. GPT partitions the disk — ESP + ext4 root on UEFI, BIOS-boot + ext4 on legacy
2. copies the running live filesystem across with `rsync` (no network needed,
   and what gets installed is exactly what you just tried)
3. generates `/etc/fstab`
4. rebuilds the initramfs **without** archiso's squashfs hooks — the live
   initramfs cannot boot a normal disk, so this step is what makes it bootable
5. strips the live-only services
6. installs GRUB for whichever firmware is present

Then remove the stick and reboot. Log in as `neth` / `nethos` and **change the
password immediately** with `passwd` — it is a published default.

## What is not verified

The ISO is tested by booting it in QEMU under both UEFI and BIOS, and the
installer is tested against a scratch disk in a VM. Neither I nor this
repository has tested it on your actual PC. Real hardware brings things a VM
never exercises — firmware quirks, Secure Boot, NVMe, Wi-Fi and GPU drivers.
In particular NETHOS ships no proprietary GPU drivers and no Wi-Fi
configuration tool, so expect to install those yourself.

Back up anything you care about before installing to a disk.
