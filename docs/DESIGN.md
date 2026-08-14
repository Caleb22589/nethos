# The NETHOS design doctrine

Most Linux desktops are *arranged*, not *designed*. Every element is
individually reasonable and the whole has no point of view: six category tiles
in six saturated colours, three icon styles, cards at three densities, and the
distribution's name somewhere on screen at all times.

The goal here is the opposite, and it is not "looks like macOS". It is the
discipline that makes macOS feel the way it does, which is almost entirely
about **restraint applied consistently**. Rounded corners and blur are the
easiest parts to copy and the least important.

These are rules, not suggestions. A design system without enforcement is a
palette, and a palette drifts back into arrangement the moment somebody adds a
widget.

---

## 1. One accent, and it means something

There is one accent colour. It marks *the thing you would press*, and nothing
else. Not headings, not icons, not decoration, not category tiles.

The moment a second accent appears, the first stops meaning anything. This is
the single biggest difference between the screenshot of a typical Linux app
store and a macOS one: six coloured tiles say nothing, because colour is being
used as decoration rather than as information.

Status colours — green, amber, red — are not accents. They appear only when
something is genuinely good, warning, or wrong, and never on a control.

## 2. Type carries the hierarchy, not weight and size

Three sizes on screen at once, at most. One heavy weight, used once per
surface. Hierarchy comes from **contrast and spacing**, not from making things
bigger.

Text is never pure black on white. `--ink` is `#1c2024`; pure `#000` on a light
translucent surface reads as harsh and cheap, and it is the specific thing that
makes some GNOME dialogs feel unfinished. Secondary text steps down in
*contrast* (`--ink-soft`), not in size.

## 3. Radii nest, and the maths is not optional

An inner corner inside an outer corner must satisfy:

```
inner_radius = outer_radius − padding
```

A 16px card with 8px padding holds 8px children. Get this wrong and the shape
looks subtly broken even though nobody can say why — the eye reads the gap
between two arcs as an error long before the mind names it.

Never a hard 90° corner. `--r-sm` through `--r-xl` exist so nothing invents its
own.

## 4. Depth comes from light, not from borders

A 1px grey border is how a widget toolkit separates things. Real interfaces use
**a shadow and a rim**: a bright hairline where light catches the top edge, a
darker line beneath for thickness, and a soft shadow that says how far the
surface floats.

Two shadow layers, always: a tight one for contact, a wide soft one for
distance. One shadow reads as a sticker.

## 5. Glass is an edge, not a blur

The expensive, convincing part of Apple's glass is **not** the blur. It is:

- a bright rim on the top edge, where light catches
- a dark inner line, giving the pane thickness
- a soft specular sheen down the top third

Those three cost nothing — they are gradients and hairlines. The blur behind
them is the cheap idea and the expensive computation.

This matters practically. `backdrop-filter` re-reads and re-blurs everything
behind an element on every repaint. With a working GPU it is nearly free; on
CPU it is per-frame work across the whole surface, and it is the difference
between a desktop that responds and one that does not. NETHOS therefore keys
blur off a `neth-gpu` class that the session sets only when the renderer is
real hardware. **Everything else keeps the rim, the sheen, and the
translucency — which is most of the look for none of the cost.**

If glass ever has to be chosen between "blurred" and "correctly edged", choose
the edge.

## 6. Space is on a grid, and it is generous

8px grid. Padding, gaps and offsets are multiples of it. The most common
failure in a hand-built interface is spacing that is *nearly* consistent —
14px here, 16px there — which reads as sloppiness without being visible as
error.

Crowding is what makes an interface feel like a tool. Space is what makes it
feel considered.

## 7. Motion is short, eased, and explains something

`--ease` is `cubic-bezier(0.32, 0.72, 0, 1)` — fast out, long settle. Nothing
moves linearly, because nothing in the physical world does.

140ms for something under the cursor. 320ms for something entering or leaving.
Anything longer is the interface admiring itself. Motion should explain where a
thing came from or where it went; if it explains nothing, remove it.

## 8. The system does not say its own name

No logo on boot. No distribution name on the desktop. No watermark, no
version in the corner, no branded wallpaper.

This is a stance, not an oversight. A system that announces itself is
configured *at* the user; a system that stays quiet is used *by* them. The
identity should be legible from the shape of the interface alone — the panel,
the dock, the type, the way things move. If NETHOS is recognisable only because
it says NETHOS, the design has failed.

This extends to boot: quiet kernel, no GRUB menu unless asked, no ASCII banner
at the login prompt.

## 9. Chrome earns its pixels or it goes

Every permanent element — every icon in the panel, every affordance — must
justify being on screen at all times. If it is used weekly, it belongs in the
launcher, not the panel.

The default state of the desktop is the wallpaper and almost nothing else.

## 10. Light is the default, and dark is not an inversion

The system is light because light surfaces let translucency read as glass; a
dark translucent panel is just a dark panel. When dark arrives it will be a
separate set of values, not `filter: invert()` — shadows do not invert, and
rims become darker rather than lighter.

---

## Applying this to a new surface

Before adding anything, in order:

1. Can this live in the launcher instead of on screen permanently? (Rule 9)
2. Does it need colour, or does it just want some? (Rule 1)
3. Are its radii derived from its parent's? (Rule 3)
4. Is every spacing value a multiple of 8? (Rule 6)
5. Does it use `.glass` and the shared tokens, or has it invented values?

Anything that invents its own colour, radius or spacing is a bug, the same as a
crash is a bug. It just fails more slowly.
