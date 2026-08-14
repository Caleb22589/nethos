# The online installer

**Status: designed, not built.**

A small image that boots, asks two questions, and builds NETHOS onto the disk
by downloading packages — rather than carrying a finished system to copy.

## Why the current builder is the wrong shape

`build-x86.sh` boots a **full Debian cloud image** and works inside it. That is
right for building on a developer's machine and wrong for an installer: it is
~460MB of a whole other distribution, with systemd, apt, cloud-init and a login
prompt, none of which an installer needs.

An installer needs exactly five things:

1. a kernel that boots on arbitrary hardware
2. enough userland to partition a disk and make filesystems
3. Python, because npkg is Python
4. a network
5. something to look at while it works

## The shape: initramfs only, no root filesystem

The installer should be a **kernel and an initramfs, and nothing else**. Linux
boots the initramfs into RAM and runs `/init`; if everything needed is in
there, no root filesystem is ever mounted. That removes the bootloader-plus-
root-partition dance and makes the whole image one file.

```
vmlinuz                    ~12MB
initrd.img                 ~80-120MB compressed
  ├── busybox              shell, mount, basic tools
  ├── python3-minimal      npkg is Python; this is the floor
  ├── parted, e2fsprogs, dosfstools
  ├── curl + ca-certificates
  ├── kernel modules       storage, network, GPU
  └── npkg + payload
```

**Realistic total: 130-180MB.** Comparable to Debian's netinst, and about a
fifth of what booting a Debian cloud image costs.

The floor is Python. npkg is ~5000 lines of it, and `python3-minimal` with its
standard library is ~40MB — the single largest thing in the image after the
kernel modules. Rewriting npkg in C to save 40MB would be a poor trade.

Kernel modules are the other weight, and the one place not to economise: an
installer that boots on the developer's machine and not on the user's is
worthless. `MODULES=most` is the right call here even though it is most of the
initramfs.

## What it does

1. Boot. No menu, no logo, no distribution name (`docs/DESIGN.md`, rule 8).
2. Bring up the network — DHCP, or ask if there is no link.
3. Show the disks and ask which one. **This is the only destructive question,
   and the only one that must be asked plainly.**
4. Partition: ESP + root. Same layout `build-x86.sh` produces.
5. Run `npkg_bootstrap` against the mounted target — the *same* code path the
   image build uses, which is why this is not a second installer to maintain.
6. chroot for initramfs, GRUB, caches.
7. Ask for a username and password.
8. Reboot.

Steps 4-6 already exist and are tested. The new work is 1-3, 7 and the display.

## The display, without a desktop

A GUI usually means a compositor, a toolkit and a font stack — several hundred
megabytes to draw a progress bar, which would double the image.

**Draw to the framebuffer instead.** `/dev/fb0` exists as soon as the kernel
has a display driver: a memory-mapped array of pixels. Python can `mmap` it and
write RGB directly. No X, no Wayland, no GTK, no toolkit — a few hundred lines
for a full-screen, properly typeset progress screen.

That also suits the design doctrine better than a toolkit would. An installer
drawn with the same restraint as the desktop — one accent, generous space, no
branding — says more about the system than a logo ever would. It is the first
thing anyone sees; it should look like NETHOS rather than like a wizard.

Text needs a bitmap font baked in, since fontconfig and freetype are not worth
carrying. One good sans at two sizes, rendered to PNG at build time and
unpacked as raw pixels, is a few hundred KB.

Fallback: if `/dev/fb0` is missing, print the same steps as plain text. An
installer that cannot draw must still install.

## What has to be built

| | |
| --- | --- |
| `installer` package set | the minimal userland listed above |
| `scripts/build-installer.sh` | assemble kernel + initramfs into one image |
| `/init` | the installer itself, in Python |
| framebuffer display | mmap `/dev/fb0`, blit a bitmap font, draw progress |
| disk picker | list disks with sizes and models, confirm destructively |

The bootstrap, partitioning and chroot steps are reused, not rewritten. That is
the point: the installer and the image build must not drift apart, and they
will not if they call the same code.

## Sizes, honestly

| | |
| --- | --- |
| kernel | ~12MB |
| modules (`MODULES=most`) | ~40MB compressed |
| python3-minimal + stdlib | ~40MB |
| busybox, parted, e2fsprogs, dosfstools | ~15MB |
| curl, ca-certificates | ~5MB |
| npkg + payload + font | ~5MB |
| **total** | **~120-150MB** |

Then it downloads ~500MB of packages during the install, which is what makes it
an online installer rather than a disc.
