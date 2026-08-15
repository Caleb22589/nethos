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

---

# The boutique details

Rules 1-10 stop the interface looking generic. They do not, on their own, make
it look *expensive*. That comes from a smaller set of details that almost
nobody implements, which is exactly why implementing them reads as care.

None of these cost rendering time. They are all decisions.

## 11. Numbers must not shimmer

A clock whose digits are proportionally spaced jitters every second as the
glyph widths change, and the eye catches it even when the mind does not. Every
number that updates — the clock, the memory readout, a percentage, a file size
— gets `font-variant-numeric: tabular-nums`.

This is the single cheapest thing on this list and the most noticeable. An
interface where the clock is perfectly still feels *built*.

## 12. Letter-spacing changes with size

Type designers space a headline tighter than body text, because at large sizes
the gaps between letters look bigger. Cheap interfaces use one tracking value
everywhere.

```
22px and up   letter-spacing: -0.02em    tighten
15-21px       letter-spacing: -0.01em
13-14px       letter-spacing: 0          body, leave it alone
11-12px caps  letter-spacing:  0.07em    small caps need air
```

## 13. One light source, and it is above

Every shadow in the system falls the same direction — straight down, never
sideways, never up. A panel lit from above and a card lit from the left cannot
exist in the same world, and the eye notices the contradiction before the mind
finds it.

The rim highlight is on the *top* edge for the same reason. Light comes from
above; that is where it catches.

## 14. Optical alignment, not mathematical

A circle centred by its bounding box looks low. A triangle centred by its
bounding box looks left. Icons next to text should be aligned so they *look*
centred, which is usually 1px off from what the maths says.

Where the eye and the number disagree, the eye is right.

## 15. Hairlines are a colour, not a width

A 1px border at full opacity is a line drawn *on* the interface. A 1px border
at 20% opacity is an edge *in* it. Never make a separator thinner to make it
subtler — make it fainter. Thin lines disappear entirely at some scale factors;
faint ones degrade gracefully.

## 16. One easing curve, everywhere

`cubic-bezier(0.32, 0.72, 0, 1)`. Every transition in the system uses it. Mixed
easings are the interface equivalent of mixed fonts: individually fine, and
collectively noise.

Duration varies with distance travelled, never the curve.

## 17. Nothing appears instantly, nothing lingers

Something that pops into existence looks like a bug. Something that fades for
half a second looks slow. 140ms in, and it should already be settling.

## 18. Empty states are designed, not left over

"No headlines" and "Loading…" are seen more often than the populated state
during the first hour of use. They get the same attention: real type, real
spacing, a sentence that says what to do rather than what went wrong.

An interface is judged in its empty state, because that is what a new user
sees first.

## 19. The signature is one thing, not five

Every boutique product has one detail it repeats. Not a logo — a *treatment*.
Here it is the glass edge: bright rim above, dark line below, sheen down the
top third. It appears on the panel, the dock, the launcher, every card, and it
is the reason a screenshot is recognisable with no name on it.

Resist adding a second signature. Two is a style guide; one is an identity.

## 20. Restraint is visible

The temptation, always, is to add. A gradient here, an icon there, a little
animation on the thing nobody looks at. Each is defensible and the sum is
noise.

What makes something look expensive is the number of things that were left
out — and that is legible to the viewer even though they could not name a
single omission.

## 21. The signature face is display only

Copperplate is the NETHOS display face: section labels, the wordmark, anything
carrying identity rather than information.

It is never body text, and the reason is structural rather than taste. It is an
engraver's type — cut for business cards and stationery — with no true
lowercase, wide proportions, and flared stroke ends that are its whole
character and that disappear below about 14px. A file list set in Copperplate
is unreadable; a section label set in it is unmistakable.

Two faces, and only two: `--display` for identity, `--sans` for everything a
person actually reads. A third would make it a style guide again (rule 19).

Copperplate Gothic is licensed and not in Debian, so `--display` falls through
to Cinzel and then to any serif. The interface is correct without it; it is
*ours* with it. To install the real thing:

```bash
sudo cp CopperplateGothic*.ttf /usr/local/share/fonts/
sudo fc-cache -f
nethos-reload
```

---

# Why ricer desktops look like concepts

A ricer designs a screenshot. An operating system designs a workday. The
screenshot is optimised for the moment it is photographed; nobody spends eight
hours in it. That difference produces a consistent set of tells.

## 22. Monospace in the interface is the loudest amateur signal

Nothing marks a desktop as homemade faster than a menu, a clock or a window
title set in a monospace font. No shipping operating system does it — macOS,
Windows and Android all use a proportional humanist sans, because that is what
reads well at 13px in a list.

Monospace belongs to code, terminals and columns of figures. NETHOS uses it for
numbers only, where the fixed width is doing actual work.

Nerd Font icon glyphs in the UI are the same tell, one step further.

## 23. Saturated accents look like software that is not finished

`#00ff88` and `#ff00aa` do not appear in shipping operating systems, and the
reason is not conservatism. A saturated accent vibrates against text, cannot
carry small type at accessible contrast, and exhausts the eye across a working
day.

Every serious accent is desaturated and slightly dark: a blue that is nearly
grey until you look at it. If the colour is memorable on its own, it is wrong.

## 24. Animation you notice is animation that failed

Ricer motion announces itself: bounces, springs, overshoot, windows that wobble.
It reads as a toy because a toy is the only thing that behaves that way.

Serious motion is 150–200ms, eased out, no overshoot, and you should be unable
to describe it afterwards. Its job is to explain where a thing went, then get
out of the way. Anything you can admire is too slow.

## 25. Decoration is not interface

Audio visualisers, circular CPU gauges, album art at a third of the screen,
animated wallpapers behind the work. Every one is a thing to look at rather
than a thing to use, and each one is a permanent tax on attention.

**This is the rule NETHOS is currently closest to breaking.** The desktop
widgets — the system monitor, the news feed, the ticker — are the same idea as
conky, and they are on screen at all times whether or not anyone is reading
them. They earn their place only if they are genuinely glanced at; if they are
there because they look good in a screenshot, they are decoration and rule 9
already says they go.

## 26. Density is what work looks like

Ricer desktops are sparse because sparse photographs well. Real operating
systems are dense: small type, tight lists, many rows visible, because the
person is looking for one file among four hundred.

Generous *space between groups*, tight *within* them. A file list with 40px
rows is a concept; the same list at 22px is a tool.

## 27. It must survive the ugly parts

Anyone can design a desktop with one terminal and a wallpaper. A serious system
also has to look right with a file dialog open, sixteen windows in the
switcher, a progress bar, an error, a password prompt, a printer queue.

Concepts never show those screens, which is exactly why they look effortless.
If a design has only been tested on an empty desktop, it has not been tested.

## 28. The desktop is empty because it is a desk

Not a canvas, not a display. The default state is a surface with nothing on it,
because everything on it is something the user did not put there.

---

# What macOS is actually doing

Reading a Tahoe screenshot closely, because the lesson is not the blur.

## 29. The interface has no colour; the content does

Every colour in that screenshot comes from the photograph and the app icons.
The glass is neutral — white and grey translucency, nothing else. The system
never tints itself.

This is the whole trick, and it is the opposite of ricing. A coloured interface
must be matched to a wallpaper and breaks when the wallpaper changes; a
colourless one takes its character from whatever is behind it and is correct on
every background. It is also why Apple can ship one look to two hundred million
machines with different wallpapers.

Our accent is for controls only (rule 1). Surfaces stay neutral. A tinted panel
is a mistake even when the tint is subtle.

## 30. Contrast is high where it carries meaning

The glass is soft; the text on it is not. Near-black on light glass, full
weight, no compromise. Softness is a property of *surfaces*, never of the words
on them.

The commonest failure in glass interfaces is letting the translucency argue
with legibility and settling on grey text at 60% — which is unreadable at a
glance and looks unfinished. If text is hard to read, fix the surface behind
it, never the text.

## 31. Corners are continuous, not circular

Apple's rounding is a superellipse — curvature that eases into the straight
edge — where CSS `border-radius` is a circular arc that meets the edge at a
tangent discontinuity. The eye reads the arc version as slightly cheap without
being able to say why, and it is one of the largest differences between a
copy and the real thing.

`corner-shape: squircle` is arriving in CSS; until WebKit has it, the practical
approximation is a *larger* radius than feels right (Apple's are bigger than
people expect) and never mixing radii on one surface.

## 32. Widgets are allowed; competing is not

Correcting rule 25: macOS does put widgets on the desktop, so the tell was
never their existence. It is their treatment.

Apple's are monochrome, low contrast, edge-aligned, and recede completely until
looked at. Ours have coloured headings and live figures that pull the eye
constantly. A widget should be *legible when sought and invisible when not*.

Desaturate them, reduce contrast, align them to one edge, and they stop being
decoration and become what they are on macOS: something you glance at.

## 33. The dock is icons, and nothing else

No labels, no text, no running indicators shouting. Consistent optical size,
one mask shape, one visual weight. Every icon is the same size *optically*,
which is not the same as the same size mathematically.

Text in a dock is the tell that the icons are not recognisable enough — and
the cure is better icons, not labels.
