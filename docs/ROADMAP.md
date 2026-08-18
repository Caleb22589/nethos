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
- [x] File explorer. Places, breadcrumbs, rename, trash, and copy/move/delete
      with an internal clipboard; opens at a folder when launched with one.
- [x] Archive extractor, as a verb on the file rather than an application:
      "Extract here" on an archive, with progress on the same event bus.
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

## 4b. Not done, and why

- [ ] **Spotify viewer.** Needs a Spotify developer application and OAuth
      credentials that only you can create; a viewer without them is a window
      that says "not configured". Worth doing once those exist.
- [ ] **Offline installer.** The image can already be built with everything
      included; what is missing is the installer preferring local packages
      over the network.
- [ ] **A/B partitions.** Snapshots cover a bad NETHOS update. They do not
      cover a kernel that will not boot, which is what the partition scheme
      in docs/ABUPDATE.md is for.
- [ ] **GTK4 window decoration.** GTK4 ignores GTK_CSD=0 and keeps its own
      header bars. Upstream's decision, not a missing setting.

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


## 6. Requested 2026-08-18, reconciled against the tree

The list as given was about forty items. Roughly a dozen of them are already
built and were reported missing because they did not work -- which this
project keeps producing and which is worth stating as a rule: on a desktop
with no terminal, *absent* and *broken* are the same picture. Four separate
faults behind one report of "ghosting" in a single session, none of them the
thing that was reported.

### Already built (verify before rebuilding)

Snapshots and rollback, the updater, desktop icons, wallpapers, the loading
splash, the control centre (Wi-Fi, brightness, battery), onboarding, the App
Store including drivers, the online installer's interface, window chrome for
NETHOS and foreign windows, the file explorer and the extractor, and the shell
memory work (1.69GB -> 392MB).

### Blocked on one thing, and it is not new

**npkg triggers.** Already section 1's largest correctness gap, and the list
above is downstream of it more than of any missing feature. The count is now
six: the `netdev` group, `regulatory.db`, `/etc/nsswitch.conf`, the `polkitd`
user, the missing XDG user directories (no ~/Desktop, so the desktop had no
icons and Files had no sidebar), and a build that died outright in
fix_alternatives. Every one presented as something else -- broken wifi, broken
DNS, dead power buttons, a broken file manager, a broken build. Nothing else
on this list buys as much as making maintainer scripts run, or emulating the
handful of triggers that matter.

### New, in rough order of leverage

- [ ] **A declarative system manifest.** One file listing every package and
      setting, so a machine can be handed over or rebuilt from it. Cheap here
      because npkg already resolves from a set, and it makes the offline
      installer and the "hand off the system" ask the same feature.
- [ ] **Troubleshooter with an AI mode.** Section 3c already wants a
      troubleshooter that can restart the shell and read diagnostics without a
      terminal. NETHBot is the natural engine: a local model, on-device, that
      already drives a shell with a human-in-the-loop pause on anything that
      asks for a password. Offline recovery and the kernel-panic assistant are
      the same tool reached from a different place, and the second one needs
      A/B partitions (docs/ABUPDATE.md) before it has anywhere to boot from.
- [ ] **Window bars on the terminal.** Already section 3b; foot has a `csd`
      setting and this is a configuration change, not a project.
- [ ] **Right-click and dock buttons.** Reported not working. Section 0 records
      this as resolved in c8fba66, so either it regressed or the report is of
      something adjacent. Testable directly now: injected pointer events plus
      the daemon's request log is what found the launcher/control-centre bug,
      and it is the only method here that exercises the button rather than the
      API behind it.
- [ ] **More settings, more customisation.** Already section 2; the
      schema-driven form makes each one cheap.
- [ ] **Applications in their own repositories**, fetched at build time rather
      than shipped in the payload. The npk format already exists for this.
- [ ] Music player with album art; camera with face detection; a WebKit
      browser; game mode; VPN setup; printing; phone and device integration
      (AirPods, AirDrop-style transfer). All real, none blocking, and each one
      is an application rather than a change to the system.
- [ ] Kernel and scheduling work -- CachyOS or Ubuntu Studio kernels, a swap
      manager, VRAM as swap, CPU scheduling. Worth measuring before building:
      section 5 has ~14s of boot time NETHOS actually owns, and the last
      memory win came from one process change rather than a scheduler.

### Not started, and honest about why

- [ ] **ARM.** `build-image.sh` targets arm64 and `build-arm.sh` exists, so
      this is not from zero -- but nothing has booted on real ARM hardware and
      no claim should be made until it has.
