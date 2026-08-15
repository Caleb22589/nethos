# NETHOS roadmap

Ordered by what blocks what, not by size. Anything marked **blocked** has a
named cause, not a guess.

## 0. RESOLVED: left-click on layer surfaces

**Cause:** the launcher's full-screen overlay surface had `opacity: 0` when
closed but never `pointer-events: none`, and nothing hid it until the launcher
had been opened once. An invisible sheet on the overlay layer swallowed every
pointer event on the screen. Fixed in c8fba66. The investigation below is kept
because the measurements were what found it.

## 0b. Original investigation: left-click on layer surfaces

The one that blocks the most, because it makes finished features look broken.

Measured on the laptop, 2026-08-15, by injecting real pointer events through
Wayfire's `stipc` plugin and watching `nethosd`'s request log:

| target | position | result |
| --- | --- | --- |
| panel launcher button | y=28, inside the 46px exclusive zone | `POST /api/launch` fires |
| context menu item | y=170, outside the exclusive zone | nothing |
| dock icon | dock sets `exclusive=0` while auto-hiding | nothing |
| window title bar close | Wayfire's own decoration | window closes |

So injected clicks work, and our surfaces receive **hover and right-click**
everywhere -- a context menu opens, and its items highlight under the pointer.
Only the left button fails, and only outside a reserved exclusive zone.
Widening the input region with `nethosHost.inputRect` while a menu is open
does not help: a click outside the menu but inside the widened rectangle does
not even dismiss it, which means the surface never saw the button at all.

Two candidates, in order of suspicion:

1. Wayfire routes button events to layer surfaces by exclusive zone rather
   than by input region. If so, dynamic input regions are the wrong mechanism
   for us entirely.
2. `set_input_region` is applied to the GdkSurface but not committed in a way
   the compositor picks up until something else forces a commit.

The fix that avoids both: give menus their own surface. The overlay surface
that hosts the launcher is full-screen, always correctly focusable, and
already receives clicks -- the launcher works. Context menus should be
rendered there and the chosen action routed back to the surface that asked
for it, over the existing event bus. That also removes the input-region
juggling from the panel and the dock.

**Already fixed on the way to finding this:** the dismissal handler closed the
menu on `pointerdown`, removing the element before `click` could fire on it,
so an item could never activate even where clicks do land.

## 1. Foundations

- [ ] **Snapshots and rollback.** Wanted in its own right and required by the
      updater below: an interrupted or bad update has to be revertible. The
      A/B partition scheme in `docs/ABUPDATE.md` is the existing sketch.
- [ ] **Updater** pulling builds from GitHub Releases, tied to snapshots so a
      failed update rolls back rather than leaving an unbootable machine.
- [ ] **npkg triggers.** The largest correctness gap. npkg unpacks packages
      but runs no maintainer scripts, so anything a `postinst` would create is
      absent. Four separate bugs came from this in one evening: the `netdev`
      group (wifi silently unavailable), a dangling `regulatory.db`
      alternatives link (no 5GHz), a missing `/etc/nsswitch.conf` (no DNS at
      all), and a missing `polkitd` user (dead power buttons). Each was
      diagnosed as broken hardware first.

## 2. Interface

- [ ] Desktop icons.
- [ ] Working wallpapers. The setting is stored and offered in Settings; the
      desktop surface does not read it yet.
- [ ] Loading screen instead of the black gap before the shell appears.
- [ ] Control centre: wifi, brightness, volume, battery.
- [ ] Battery indicator and animations.
- [ ] More customisation, and more settings behind the Settings app now that
      the schema-driven form makes adding one cheap.
- [ ] Onboarding on first boot.

## 3. Applications

**The App Store is the big one** -- stated priority, and it subsumes several
other items: driver installation becomes an App Store category rather than a
separate tool, and npkg already resolves capabilities well enough to back it.

- [ ] App store, including auto-installing drivers.
- [ ] File explorer.
- [ ] Archive extractor.
- [ ] Spotify viewer and similar helpers.

All of them want a shared window chrome and a list/grid component first, or
each will invent its own and the family resemblance will be lost. That shared
chrome is also the answer to inconsistent window bars below.

## 3b. Window decoration

- [x] NETHOS windows draw their own chrome: rounded corners, three traffic
      lights on the left with the symbol revealed on hover, centred title,
      38px bar. Built in GTK rather than HTML so dragging and resizing are the
      compositor's, not ours.
- [x] Foreign windows match, via firedecor -- which *is* in Debian, as
      `reform-firedecor`, despite the name. It has the corner radius and the
      layout string Wayfire's built-in decorator lacks, so Chromium, Thunar
      and the terminal wear the same rounded frame and the same three lights.
      No C++ needed after all.
- [ ] GTK4 applications still draw their own header bars and ignore
      GTK_CSD=0. That is the remaining gap, and it is upstream's decision
      rather than a missing setting.
- [ ] Window bars are inconsistent, and absent on the terminal. Applications
      that draw their own decorations (client-side) ignore Wayfire's, so a
      NETHOS session shows two different title bars depending on the toolkit.
      Force server-side decoration where the application allows it, and
      configure the ones that do not (foot has a `csd` setting).

## 3c. Troubleshooting

- [ ] A troubleshooter that can restart and diagnose the interface without a
      terminal: restart the shell, restart nethosd, reload surfaces, show the
      diagnostics `nethos-doctor` already collects. Every UI fault in this
      project so far has been invisible from the desktop itself.

## 4. Installer

- [ ] Online installer, light, with a proper UI.
- [ ] Offline image for machines with no network interface.

## 5. Performance

- [x] Shell memory 1.69GB -> 392MB, by sharing one web process again.
- [x] `xdg-desktop-portal` 25.2s -> gone, by installing the gtk backend.
- [x] Duplicate network stacks (NetworkManager *and* systemd-networkd).
- [ ] Remaining boot time. Of ~28s to a usable desktop, 13.3s is the laptop's
      own firmware and cannot be touched from here. Kernel is 3.5s, userspace
      to `multi-user.target` 4.9s, then ~5s to the shell. The ~14s NETHOS owns
      is what is left to attack.
- [ ] Memory again, after the above: the shell is no longer the largest
      consumer, so the next measurement should come before the next change.
