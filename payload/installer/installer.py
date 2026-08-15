#!/usr/bin/env python3
"""
The NETHOS installer.

Runs as PID 1 from an initramfs: there is no root filesystem, no systemd, and
nothing else running. It brings up a network, asks which disk to use, and then
builds NETHOS onto it with the same npkg_bootstrap the image build uses.

Drawing: straight to /dev/fb0, which is a memory-mapped array of pixels as soon
as the kernel has a display driver. No X, no Wayland, no toolkit -- a GUI stack
would be several hundred megabytes to draw a progress bar, on an image whose
whole point is being small. Text comes from a PSF console font, which the
kernel ships anyway.

If there is no framebuffer, everything falls back to plain text. An installer
that cannot draw must still install.
"""

import os
import struct
import subprocess
import sys
import time

sys.path.insert(0, "/nethos/pkg")

# The design doctrine, in six values. docs/DESIGN.md.
INK        = (0x1c, 0x20, 0x24)
INK_SOFT   = (0x5a, 0x64, 0x70)
INK_FAINT  = (0x8b, 0x94, 0xa0)
# Four bands, top to bottom: the wallpaper the desktop will have, reduced to
# what a framebuffer can draw cheaply.
BACKDROP   = ((0xee, 0xf2, 0xf8), (0xea, 0xef, 0xf6),
              (0xe6, 0xeb, 0xf4), (0xe1, 0xe7, 0xf1))
SHADOW     = ((0xdc, 0xe2, 0xea), (0xe4, 0xe9, 0xf0), (0xea, 0xee, 0xf4))
ACCENT     = (0x2f, 0x7c, 0xf6)
BG         = (0xf2, 0xf4, 0xf7)
SURFACE    = (0xff, 0xff, 0xff)


# ---------------------------------------------------------------- framebuffer

class Screen:
    """A framebuffer, or nothing at all.

    Text is drawn from a PSF font because the alternative -- freetype and
    fontconfig -- is tens of megabytes to render a dozen strings.
    """

    def __init__(self):
        self.fb = None
        self.w = self.h = 0
        try:
            with open("/sys/class/graphics/fb0/virtual_size") as fh:
                self.w, self.h = (int(v) for v in fh.read().strip().split(","))
            with open("/sys/class/graphics/fb0/bits_per_pixel") as fh:
                self.bpp = int(fh.read().strip())
            if self.bpp != 32:
                raise ValueError("only 32bpp is handled")
            self.fb = open("/dev/fb0", "r+b", buffering=0)
            import mmap
            self.mem = mmap.mmap(self.fb.fileno(), self.w * self.h * 4)
        except (OSError, ValueError, IndexError):
            self.fb = None
        self.font = self._load_font()

    # -- font -------------------------------------------------------------
    def _load_font(self):
        """Parse a PSF console font into {codepoint: [row bitmasks]}."""
        for path in ("/nethos/font.psf",
                     "/usr/share/consolefonts/Lat15-Terminus16.psf.gz",
                     "/usr/share/kbd/consolefonts/default8x16.psfu.gz"):
            try:
                data = open(path, "rb").read()
                if data[:2] == b"\x1f\x8b":
                    import gzip
                    data = gzip.decompress(data)
            except OSError:
                continue
            try:
                if data[:2] == b"\x36\x04":                    # PSF1
                    height = data[3]
                    glyphs, start, count = {}, 4, 256
                elif data[:4] == b"\x72\xb5\x4a\x86":          # PSF2
                    _, _, _, hdr, _, count, height, width = \
                        struct.unpack("<IIIIIIII", data[:32])
                    start = hdr
                    glyphs = {}
                else:
                    continue
                size = height                                   # 1 byte/row
                for i in range(count):
                    off = start + i * size
                    glyphs[i] = list(data[off:off + size])
                return {"h": height, "w": 8, "g": glyphs}
            except (struct.error, IndexError):
                continue
        return None

    # -- primitives -------------------------------------------------------
    def fill(self, x, y, w, h, rgb):
        if not self.fb:
            return
        x, y = max(0, x), max(0, y)
        w, h = min(w, self.w - x), min(h, self.h - y)
        if w <= 0 or h <= 0:
            return
        row = bytes((rgb[2], rgb[1], rgb[0], 0)) * w
        for line in range(y, y + h):
            off = (line * self.w + x) * 4
            self.mem[off:off + len(row)] = row

    def text(self, x, y, s, rgb=INK, scale=1):
        if not self.fb or not self.font:
            return
        fh, fw = self.font["h"], self.font["w"]
        for ch in s:
            rows = self.font["g"].get(ord(ch))
            if rows:
                for ry, bits in enumerate(rows):
                    for rx in range(fw):
                        if bits & (0x80 >> rx):
                            self.fill(x + rx * scale, y + ry * scale,
                                      scale, scale, rgb)
            x += fw * scale

    def clear(self):
        self.fill(0, 0, self.w, self.h, BG)


# ------------------------------------------------------------------- shell

def run(argv, check=True, capture=False):
    """Run a command, and say what failed rather than dying silently."""
    try:
        if capture:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=1800)
            if check and out.returncode != 0:
                raise RuntimeError("%s: %s" % (argv[0], out.stderr.strip()[:200]))
            return out.stdout
        code = subprocess.call(argv)
        if check and code != 0:
            raise RuntimeError("%s exited %d" % (argv[0], code))
        return ""
    except FileNotFoundError:
        raise RuntimeError("%s is not in the installer image" % argv[0])


def disks():
    """Whole disks worth installing to, largest first."""
    found = []
    for name in sorted(os.listdir("/sys/block")):
        if name.startswith(("loop", "ram", "sr", "fd", "dm-", "md")):
            continue
        try:
            sectors = int(open("/sys/block/%s/size" % name).read())
            if sectors < 8 * 1024 * 1024 * 2:      # under 8GB: not a target
                continue
            model = ""
            for attr in ("device/model", "device/name"):
                try:
                    model = open("/sys/block/%s/%s" % (name, attr)).read().strip()
                    break
                except OSError:
                    pass
            removable = open("/sys/block/%s/removable" % name).read().strip() == "1"
            found.append({
                "dev": "/dev/" + name,
                "gb": sectors * 512 / 1e9,
                "model": model or "disk",
                "removable": removable,
            })
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda d: -d["gb"])


# --------------------------------------------------------------------- UI

class UI:
    """The whole interface: a title, a step, a bar, and a line of detail.

    Deliberately not a wizard. One accent, generous space, no logo -- this is
    the first thing anyone sees of NETHOS and it should look like the desktop
    it is about to install. docs/DESIGN.md.
    """

    def __init__(self, screen):
        self.s = screen
        self.step = ""
        self.detail = ""
        self.pct = 0.0

    def draw(self):
        s = self.s
        if not s.fb:
            return
        s.clear()

        # A gradient rather than a flat fill. Four bands is enough at this
        # size and costs four rectangle fills; a per-pixel gradient on a
        # framebuffer with no GPU is thousands of writes per frame, and this
        # redraws on every progress update.
        for i, band in enumerate(BACKDROP):
            s.fill(0, i * s.h // len(BACKDROP), s.w,
                   s.h // len(BACKDROP) + 1, band)

        cx = s.w // 2
        card_w = min(760, s.w - 96)
        card_h = 300
        cx0, cy0 = cx - card_w // 2, s.h // 2 - card_h // 2

        # A soft shadow: three progressively lighter rings rather than one
        # hard offset rectangle, which reads as a drop shadow from 1995.
        for i, tone in enumerate(SHADOW):
            s.fill(cx0 - i, cy0 + 4 + i * 2, card_w + i * 2, card_h, tone)
        s.fill(cx0, cy0, card_w, card_h, SURFACE)
        # The rim: one hairline along the top, which is what makes a flat
        # rectangle read as a lit surface.
        s.fill(cx0, cy0, card_w, 1, (0xff, 0xff, 0xff))

        pad = 46
        scale = 2 if s.w >= 1280 else 1
        fh = (s.font["h"] if s.font else 16)

        # The mark. Same one the shell uses, and the only colour up here.
        dot = 10 if scale == 1 else 14
        s.fill(cx0 + pad, cy0 + 40, dot, dot, ACCENT)

        s.text(cx0 + pad, cy0 + 78, "NETHOS", INK, scale)
        s.text(cx0 + pad, cy0 + 78 + fh * scale + 22, self.step[:52], INK, 1)
        s.text(cx0 + pad, cy0 + 78 + fh * scale + 22 + fh + 8,
               self.detail[:72], INK_FAINT, 1)

        # Progress: a rail and a fill, with the percentage on the same line as
        # the rail rather than under it, so the eye reads one row not two.
        bar_y = cy0 + card_h - 74
        bar_w = card_w - pad * 2 - 62
        s.fill(cx0 + pad, bar_y, bar_w, 6, (0xe4, 0xe9, 0xf0))
        filled = int(bar_w * max(0.0, min(1.0, self.pct)))
        if filled:
            s.fill(cx0 + pad, bar_y, filled, 6, ACCENT)
        s.text(cx0 + pad + bar_w + 18, bar_y - 4,
               "%3d%%" % int(self.pct * 100), INK_SOFT, 1)

        # The one line that matters if something goes wrong at 3am.
        s.text(cx0 + pad, cy0 + card_h - 34,
               "Do not power the machine off", INK_FAINT, 1)

    def say(self, step=None, detail=None, pct=None):
        if step is not None:
            self.step = step
        if detail is not None:
            self.detail = detail
        if pct is not None:
            self.pct = pct
        if self.s.fb:
            self.draw()
        else:
            print("%-40s %s" % (self.step, self.detail), flush=True)


# ---------------------------------------------------------------- install

def network(ui):
    ui.say("Connecting", "bringing up the network", 0.02)
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo":
            continue
        run(["ip", "link", "set", name, "up"], check=False)
    for _ in range(20):
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            run(["udhcpc", "-i", name, "-n", "-q", "-t", "3"], check=False)
            try:
                if open("/sys/class/net/%s/operstate" % name).read().strip() == "up":
                    return True
            except OSError:
                pass
        time.sleep(1)
    return False


def partition(ui, dev):
    ui.say("Preparing the disk", dev, 0.06)
    run(["wipefs", "-a", dev])
    run(["parted", "-s", dev, "mklabel", "gpt"])
    run(["parted", "-s", dev, "mkpart", "ESP", "fat32", "1MiB", "513MiB"])
    run(["parted", "-s", dev, "set", "1", "esp", "on"])
    run(["parted", "-s", dev, "mkpart", "root", "ext4", "513MiB", "100%"])
    time.sleep(2)
    run(["partx", "-u", dev], check=False)
    p = (dev + "p") if dev[-1].isdigit() else dev
    run(["mkfs.fat", "-F32", "-n", "NETHOSEFI", p + "1"])
    run(["mkfs.ext4", "-q", "-F", "-L", "NETHOS", p + "2"])
    os.makedirs("/target", exist_ok=True)
    run(["mount", p + "2", "/target"])
    os.makedirs("/target/boot", exist_ok=True)
    run(["mount", p + "1", "/target/boot"])
    return p


def bootstrap(ui, user, password):
    """The same code the image build runs. Not a second installer."""
    offline = os.environ.get("NETHOS_OFFLINE", "")
    ui.say("Installing NETHOS",
           "installing from the image" if offline else
           "downloading and converting packages", 0.12)
    import npkg_bootstrap

    original = npkg_bootstrap.say
    state = {"n": 0}

    def relay(*args, **kwargs):
        line = " ".join(str(a) for a in args).strip()
        original(*args, **kwargs)
        if not line:
            return
        # The bootstrap prints "  123/548" as it goes; turn that into the bar
        # rather than inventing a fake one.
        if "/" in line and line.replace("/", "").replace(" ", "").isdigit():
            done, total = (int(v) for v in line.split("/"))
            if total:
                state["n"] = done / total
                ui.say(detail=line, pct=0.12 + 0.68 * state["n"])
        else:
            ui.say(detail=line[:64], pct=0.12 + 0.68 * state["n"])

    npkg_bootstrap.say = relay
    try:
        npkg_bootstrap.bootstrap(
            root="/target",
            sets=["base", "system", "kernel", "desktop", "firmware"],
            arch=os.environ.get("NETHOS_ARCH", "amd64"),
            username=user, password=password, root_password=password,
            hostname="nethos",
            # Offline images carry their converted packages on their own
            # partition; pointing the bootstrap's work directory at them means
            # every package is already present and nothing is downloaded. The
            # machine with no network driver is exactly the machine that cannot
            # download a network driver.
            work=(offline or "/tmp/nethos-work"),
            mirror=npkg_bootstrap.MIRROR, suite=npkg_bootstrap.SUITE,
            keep=False)
    finally:
        npkg_bootstrap.say = original


def finalise(ui, dev):
    ui.say("Making it bootable", "initramfs and bootloader", 0.86)
    for src, dst in (("/dev", "/target/dev"), ("/proc", "/target/proc"),
                     ("/sys", "/target/sys")):
        os.makedirs(dst, exist_ok=True)
        run(["mount", "--bind", src, dst], check=False)
    script = "/target/root/finish.sh"
    with open(script, "w") as fh:
        fh.write("""#!/bin/bash
set -eu
KVER=$(ls /usr/lib/modules | head -1)
depmod -a "$KVER" || true
update-initramfs -c -k "$KVER"
grub-install --target=x86_64-efi --efi-directory=/boot \\
    --bootloader-id=NETHOS --removable --no-nvram
update-grub
""")
    os.chmod(script, 0o755)
    run(["chroot", "/target", "/root/finish.sh"])
    os.remove(script)
    ui.say(detail="flushing", pct=0.97)
    run(["sync"])
    run(["umount", "-R", "/target"], check=False)


def main():
    screen = Screen()
    ui = UI(screen)
    ui.say("NETHOS", "starting", 0.0)

    try:
        if not network(ui):
            ui.say("No network", "an ethernet cable or wifi is needed", 0.0)
            time.sleep(30)

        found = disks()
        if not found:
            ui.say("No disk found", "nothing at least 8GB is attached", 0.0)
            time.sleep(60)
            return 1

        # Non-interactive when told which disk; otherwise ask, because this is
        # the one irreversible decision in the whole process.
        target = os.environ.get("NETHOS_DISK", "")
        if not target:
            if screen.fb:
                ui.say("Choose a disk", "installer is non-interactive; "
                       "pass nethos.disk=/dev/sdX", 0.0)
            for d in found:
                print("  %-12s %6.0f GB  %s%s" % (
                    d["dev"], d["gb"], d["model"],
                    "  (removable)" if d["removable"] else ""))
            time.sleep(60)
            return 1

        user = os.environ.get("NETHOS_USER", "neth")
        password = os.environ.get("NETHOS_PASS", "nethos")

        p = partition(ui, target)
        bootstrap(ui, user, password)
        finalise(ui, target)

        ui.say("Done", "remove the installer and restart", 1.0)
        time.sleep(5)
        run(["reboot", "-f"], check=False)
        return 0

    except Exception as exc:                       # noqa: BLE001
        ui.say("Installation failed", str(exc)[:64], ui.pct)
        print("\n%s" % exc, file=sys.stderr)
        # Stay up so the message can be read, and so a shell is reachable on
        # another VT rather than the machine simply resetting.
        time.sleep(600)
        return 1


if __name__ == "__main__":
    sys.exit(main())
