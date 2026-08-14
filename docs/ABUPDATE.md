# A/B updates

**Status: designed, not built.** Today `nethos-update` replaces files on the
running system. This describes what should replace it, and why it is not a
weekend change.

## What is wrong with updating in place

`nethos-update` copies the payload over the live system. It takes seconds and
it is genuinely useful, but:

- **It is not atomic.** Power loss halfway leaves a system that is partly the
  old version and partly the new one, with no record of which files are which.
- **There is no way back.** A change that breaks the desktop breaks the machine
  you would use to fix it.
- **It cannot update anything that is in use** — the kernel, libc, systemd.
  Those are exactly the updates most likely to go wrong.

The last point is the real limit. In-place updating can never be more than a
way to ship shell changes.

## The design

Two root partitions and one shared data partition:

```
p1  ESP     512M   shared: GRUB and both kernels
p2  root A  ~4G    a complete system
p3  root B  ~4G    a complete system
p4  data    rest   /home, /var/lib/npkg, /etc/nethos
```

One slot is active; the other is idle. An upgrade never touches the running
system:

1. Read the active slot from `/etc/nethos/slot`.
2. Bootstrap a **complete new system** into the idle slot — the same
   `npkg_bootstrap` the image build uses, into a mounted partition rather than
   a fresh disk.
3. chroot into it for the parts that must run inside: `depmod`, the initramfs,
   the caches, GRUB's config.
4. Flip GRUB's default to the idle slot and set a **boot counter**.
5. Reboot.

On the next boot the new slot marks itself good. If it does not — three failed
attempts — GRUB falls back to the slot that worked. A broken update costs a
reboot, not a reflash.

The user's data never moves, because it was never in either slot.

## Why this is not a small change

**The partition layout is decided at flash time.** Adopting A/B means
reflashing, and it roughly doubles the image: two ~2.5G systems instead of one.
A 6G image becomes 10-12G.

**`/etc` has to be split.** Machine identity — the machine-id, ssh host keys,
network configuration, the theme setting — has to live on the data partition
and be bind-mounted, or it is lost on every update. Deciding what is
configuration and what is system state is the fiddly part, and getting it wrong
means an upgrade silently forgets the wifi password.

**The bootloader has to count.** GRUB needs `GRUB_DEFAULT=saved`, per-slot
menu entries, and boot-success marking from inside the booted system. An
unfinished version of this is worse than none: a bootloader that flips slots
but never marks success will ping-pong between two systems forever.

**The upgrade must be resumable.** Bootstrapping into a slot takes minutes and
can be interrupted. A half-written slot must be detectable and discardable,
which means a state file and a flag that says "this slot is not finished".

## What already exists in its favour

- `npkg_bootstrap` builds a complete system into any directory, which is
  exactly step 2 — it does not care whether that directory is a fresh disk or
  a mounted partition.
- The package cache already survives between builds, so bootstrapping a slot
  downloads nothing if nothing changed.
- The chroot steps are already written and tested in `build-x86.sh`; they need
  extracting into something both the build and the upgrade can call.
- `/etc/nethos/` already exists for machine-local settings.

The pieces are there. The work is layout, bootloader logic, and being careful
about state.

## When to build it

Not while the system is changing daily. Every A/B upgrade is a full bootstrap —
minutes — where `nethos-update` is seconds, and during active development the
fast loop matters more than atomicity.

The moment to switch is when NETHOS is being *used* rather than *built*: when
an update landing badly would cost a real day, and when the kernel and libc
need updating rather than only the shell.

Until then, in-place updates for the shell and a full reflash for anything
deeper is the honest trade, and it is a normal one — it is roughly where most
distributions still are.
