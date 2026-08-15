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

- [x] **Snapshots and rollback**, as `nethos-snapshot`. Deliberately not A/B
      partitions or btrfs: those roll back the whole machine, which is right
      for a kernel that will not boot and wrong for what actually goes wrong
      here -- an update that changes a stylesheet and a daemon and leaves the
      desktop broken while the system underneath is fine. A snapshot is ~100KB
      and takes about a second. Measured: a corrupted nethos.css went 28881
      bytes -> 32 -> 28881.
- [x] **Updater** tied to it. `nethos-update` snapshots before applying and
      restores automatically if the install fails part way -- half an update
      is worse than none, because some files are new and some are old and
      nothing says which. Both are in Settings.
- [ ] Still worth having for the kernel case: A/B partitions per
      `docs/ABUPDATE.md`, for updates that can leave a machine unbootable.
- [ ] **npkg triggers.** The largest correctness gap. npkg unpacks packages
      but runs no maintainer scripts, so anything a `postinst` would create is
      absent. Four separate bugs came from this in one evening: the `netdev`
      group (wifi silently unavailable), a dangling `regulatory.db`
      alternatives link (no 5GHz), a missing `/etc/nsswitch.conf` (no DNS at
      all), and a missing `polkitd` user (dead power buttons). Each was
      diagnosed as broken hardware first.

## 2. Interface

- [x] Desktop icons, from ~/Desktop, through the same /api/files the Files app
      uses.
- [x] Wallpapers -- four, drawn rather than shipped, each with a dark form.
- [x] Loading screen: a compositor background colour so the first frame is not
      black, plus a splash surface that waits for the panel and gives up after
      ten seconds rather than hiding a failure.
- [x] Control centre: battery with time remaining, brightness, Wi-Fi.
- [x] Volume, with mute. PipeWire, pipewire-pulse and wireplumber are in the
      desktop set now; before this the machine had no sound at all.
- [ ] More customisation, and more settings behind the Settings app now that
      the schema-driven form makes adding one cheap.
- [x] Onboarding on first boot: four steps, all of them reversible in
      Settings, and it says so. Shown once, flagged in settings.json.

## 3. Applications

**The App Store is the big one** -- stated priority, and it subsumes several
other items: driver installation becomes an App Store category rather than a
separate tool, and npkg already resolves capabilities well enough to back it.

- [x] App store, including drivers. Backed by npkg: search, install and
      remove, with live output while it works. Drivers are a category rather
      than a separate tool, because they are just packages.
- [ ] File explorer.
- [ ] Archive extractor.
- [ ] Spotify viewer and similar helpers.

- [x] Shared chrome: `.app-shell`, `.app-toolbar`, `.app-search`, `.row`,
      `.tile`, `.icon-well`, `.chip`, `.empty`, `.spin`, `.stream` in
      nethos.css. The App Store is built entirely from them, so the file
      explorer and extractor should need no new furniture.

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

- [x] Online installer with a real interface. Still drawn straight to
      /dev/fb0 -- a GUI stack would be several hundred megabytes to draw a
      progress bar, on an image whose entire point is being small -- but it
      now has the wallpaper, the mark, a soft shadow and a hairline rim, and
      it still falls back to plain text where there is no framebuffer.
- [ ] Offline image. `scripts/build-x86.sh --sets "..."` already produces one
      with everything included; what is missing is the installer knowing to
      use the local packages instead of the network.

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
