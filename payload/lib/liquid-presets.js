/* NETHOS — looks for the liquid metal surfaces.
 *
 * A preset is one coherent answer to "what is this bar made of": the
 * environment it reflects, the conductor it is made of, and the CSS tokens the
 * contents sitting on it need in order to stay readable. Those three cannot be
 * chosen separately -- a darker metal needs lighter ink, and a bar tuned for a
 * pale desktop is a black stripe on a dark one -- which is why they travel
 * together rather than as six unrelated settings.
 *
 * `env` is merged over the renderer's STUDIO default, so a preset only states
 * what it changes.
 *
 * The two named "default" are what `auto` resolves to, following the theme.
 */

/* The elevation ramp is where a metal's character lives. The rule that matters
   (documented at length in lib/liquid-metal.js): the dome must not clip, or
   the bar renders as matte white plastic instead of metal. Highlights are the
   lights' job, and lights are small. */
const DARK_RAMP = [
  [-1.00, 0.000], [-0.86, 0.000], [-0.78, 0.700], [-0.66, 0.050],
  [-0.48, 0.320], [-0.30, 0.060], [-0.12, 0.010], [0.06, 0.028],
  [0.20, 0.420], [0.36, 0.500], [0.60, 0.720], [1.00, 1.050],
];

/* Lifted core and a brighter floor bounce. On a pale desktop the dark-theme
   ramp reads as a black slab laid across the top of the screen; this keeps the
   banding but raises the whole thing into steel rather than pitch. */
const LIGHT_RAMP = [
  [-1.00, 0.000], [-0.86, 0.020], [-0.78, 0.820], [-0.66, 0.120],
  [-0.48, 0.480], [-0.30, 0.180], [-0.12, 0.090], [0.06, 0.130],
  [0.20, 0.560], [0.36, 0.640], [0.60, 0.860], [1.00, 1.150],
];

export const PRESETS = {
  "chrome-dark": {
    label: "Chrome",
    env: { gradient: DARK_RAMP },
    material: { tint: [0.90, 0.905, 0.91], exposure: 1.02, contrast: 1.14, roughness: 0.012 },
    thickness: 0.78,
    pane: [8, 11, 16],
    ink: "#eaeef1",
    type: "linear-gradient(180deg,#ffffff 0%,#f6f8f9 32%,#eaeef1 56%,#ccd3d9 84%,#f4f7f9 100%)",
  },

  "chrome-light": {
    label: "Chrome (light)",
    env: { gradient: LIGHT_RAMP },
    material: { tint: [0.93, 0.935, 0.94], exposure: 1.0, contrast: 1.08, roughness: 0.014 },
    thickness: 0.78,
    // A pale pane and dark ink: on the lifted ramp the bar is bright enough
    // that light type on it has nowhere dark to sit.
    pane: [255, 255, 255],
    ink: "#1c2024",
    type: "linear-gradient(180deg,#3a4048 0%,#22262c 40%,#161a1f 70%,#2e343b 100%)",
  },

  mercury: {
    label: "Mercury",
    env: { gradient: DARK_RAMP },
    // Rounder and softer: a fatter tube and a little roughness turn the hard
    // banding into a rolled highlight.
    material: { tint: [0.95, 0.95, 0.96], exposure: 1.12, contrast: 1.05, roughness: 0.05 },
    thickness: 0.95,
    pane: [10, 12, 18],
    ink: "#eef2f6",
    type: "linear-gradient(180deg,#ffffff 0%,#f2f6f8 40%,#dfe6ec 70%,#fbfdfe 100%)",
  },

  obsidian: {
    label: "Obsidian",
    // A dark conductor. F0 well under chrome's, so most of the environment is
    // absorbed and only the hot lights survive as edges.
    env: { gradient: DARK_RAMP },
    material: { tint: [0.34, 0.35, 0.38], exposure: 1.25, contrast: 1.2, roughness: 0.02 },
    thickness: 0.8,
    pane: [4, 5, 8],
    ink: "#dfe4ea",
    type: "linear-gradient(180deg,#e8edf2 0%,#c2cad3 45%,#97a1ac 78%,#dbe2e9 100%)",
  },

  titanium: {
    label: "Titanium",
    env: { gradient: DARK_RAMP },
    // Matte and neutral: enough roughness to blur the bands into a sheen.
    material: { tint: [0.72, 0.725, 0.735], exposure: 1.06, contrast: 1.02, roughness: 0.09 },
    thickness: 0.82,
    pane: [10, 12, 15],
    ink: "#e7ebf0",
    type: "linear-gradient(180deg,#f4f6f8 0%,#dde2e7 45%,#b9c0c8 80%,#eef1f4 100%)",
  },

  brass: {
    label: "Brass",
    env: { gradient: DARK_RAMP },
    // A warm conductor tints what it reflects; the type is warmed to match, or
    // it reads as cold type lying on top of a warm bar rather than cut from it.
    material: { tint: [0.94, 0.76, 0.42], exposure: 1.05, contrast: 1.12, roughness: 0.03 },
    thickness: 0.8,
    pane: [16, 12, 6],
    ink: "#f6ecd8",
    type: "linear-gradient(180deg,#fff8e8 0%,#f3e2bd 38%,#d8be86 74%,#fdf6e6 100%)",
  },
};

export const DEFAULT_DARK = "chrome-dark";
export const DEFAULT_LIGHT = "chrome-light";

/* `auto` follows the theme, which is the only preset choice that can be right
   on a machine whose theme follows the time of day. */
export function resolve(name, dark) {
  if (!name || name === "auto") return dark ? DEFAULT_DARK : DEFAULT_LIGHT;
  return PRESETS[name] ? name : (dark ? DEFAULT_DARK : DEFAULT_LIGHT);
}
