/*
 * liquid-metal.js — WebGL2 raymarched chrome / liquid-metal renderer.
 *
 * Shapes are SDF chains (round cones along a Catmull-Rom spline) flattened on Z,
 * shaded as a smooth conductor against a procedural studio environment.
 * Each shape is drawn as one screen-space quad covering only its bounds, so cost
 * scales with covered pixels rather than with the number of shapes on screen.
 */

const MAX_SEGMENTS = 40;              // per shape
const MAX_LIGHTS = 8;
const MAX_GRAD = 16;                  // environment gradient stops

const VERT = `#version 300 es
in vec2 aPos;
uniform vec4 uRect;                   // xy = ndc min, zw = ndc max
out vec2 vNdc;
void main(){
  vec2 ndc = mix(uRect.xy, uRect.zw, aPos);
  vNdc = ndc;
  gl_Position = vec4(ndc, 0.0, 1.0);
}`;

const FRAG = `#version 300 es
precision highp float;

#define MAX_SEG ${MAX_SEGMENTS}
#define MAX_LIGHTS ${MAX_LIGHTS}
#define MAX_GRAD ${MAX_GRAD}

in vec2 vNdc;
out vec4 fragColor;

// camera
uniform vec3  uCamPos;
uniform float uTanHalf;
uniform float uAspect;

// geometry (endpoints already pre-flattened on Z by the CPU)
uniform vec4  uSeg[MAX_SEG * 2];
uniform int   uSegCount;
uniform float uFlat;                  // z squash, 1 = spherical
uniform float uSmooth;                // smin radius between segments
uniform vec4  uBound;                 // bounding sphere: xyz centre, w radius

// material
uniform vec3  uF0;                    // conductor reflectance at normal incidence
uniform float uRough;                 // widens env light falloffs
uniform int   uBounces;
uniform float uOpacity;
uniform int   uSteps;

// melt
uniform float uWobbleAmp;
uniform float uWobbleFreq;
uniform float uTime;

// environment
uniform mat3  uEnvRot;
uniform vec4  uGrad[MAX_GRAD];        // x = elevation stop, yzw = radiance
uniform int   uGradCount;
uniform vec3  uAzAmp, uAzFreq, uAzPhase;   // azimuthal panel structure
uniform vec3  uLightDir[MAX_LIGHTS];
uniform vec3  uLightCol[MAX_LIGHTS];
uniform vec4  uLightPar[MAX_LIGHTS];  // sizeX, sizeY, softness, intensity
uniform int   uLightCount;

// grade
uniform float uExposure, uContrast, uSaturation;

/* ---------------------------------------------------------------- geometry */

// cubic smooth-min: C2 continuous. The usual quadratic smin is only C1, and a
// mirror surface turns every second-derivative jump into a visible ripple.
float smin(float a, float b, float k){
  if (k <= 0.0) return min(a, b);
  float h = max(k - abs(a - b), 0.0) / k;
  return min(a, b) - h * h * h * k * (1.0 / 6.0);
}

// iq's exact round cone (tapered capsule)
float sdRoundCone(vec3 p, vec3 a, vec3 b, float r1, float r2){
  vec3  ba = b - a;
  float l2 = dot(ba, ba);
  float rr = r1 - r2;
  float a2 = l2 - rr * rr;
  float il2 = 1.0 / l2;
  vec3  pa = p - a;
  float y = dot(pa, ba);
  float z = y - l2;
  vec3  xv = pa * l2 - ba * y;
  float x2 = dot(xv, xv);
  float y2 = y * y * l2;
  float z2 = z * z * l2;
  float k = sign(rr) * rr * rr * x2;
  if (sign(z) * a2 * z2 > k) return sqrt(x2 + z2) * il2 - r2;
  if (sign(y) * a2 * y2 < k) return sqrt(x2 + y2) * il2 - r1;
  return (sqrt(x2 * a2 * il2) + y * rr) * il2 - r1;
}

float sdScene(vec3 p){
  vec3 q = vec3(p.xy, p.z / uFlat);
  float d = 1e9;
  for (int i = 0; i < uSegCount; i++){
    vec4 A = uSeg[i * 2];
    vec4 B = uSeg[i * 2 + 1];
    float di, r;
    if (B.w < 0.0){
      di = length(q - A.xyz) - A.w;
      r = A.w;
    } else {
      di = sdRoundCone(q, A.xyz, B.xyz, A.w, B.w);
      r = min(A.w, B.w);
    }
    // blend radius tracks the local tube radius, so joints stay hidden in the
    // fat body and in a thin tail alike
    d = smin(d, di, uSmooth * r);
  }

  d *= uFlat;
  if (uWobbleAmp > 0.0){
    float f = uWobbleFreq;
    d += uWobbleAmp * sin(p.x * f + uTime)
                    * sin(p.y * f * 1.31 - uTime * 0.83)
                    * sin(p.z * f * 0.77 + uTime * 1.27);
  }
  return d;
}

vec3 calcNormal(vec3 p, float e){
  vec2 k = vec2(1.0, -1.0);
  return normalize(k.xyy * sdScene(p + k.xyy * e) +
                   k.yyx * sdScene(p + k.yyx * e) +
                   k.yxy * sdScene(p + k.yxy * e) +
                   k.xxx * sdScene(p + k.xxx * e));
}

/* Near/far of the bounding sphere, .y < 0 when missed. A sphere beats a box
   slab here: it hugs the ray more tightly along t, which is what the step
   count actually depends on. The AABB earns its keep on the CPU instead,
   choosing the draw rectangle -- see _screenRect. */
vec2 boundHit(vec3 ro, vec3 rd){
  vec3 oc = ro - uBound.xyz;
  float b = dot(oc, rd);
  float c = dot(oc, oc) - uBound.w * uBound.w;
  float h = b * b - c;
  if (h < 0.0) return vec2(-1.0, -1.0);
  h = sqrt(h);
  return vec2(-b - h, -b + h);
}

bool trace(vec3 ro, vec3 rd, float tMin, out float tHit){
  vec2 tb = boundHit(ro, rd);
  tHit = 0.0;
  if (tb.y < 0.0) return false;
  float t = max(tb.x, tMin);
  float tEnd = tb.y;
  float relax = uWobbleAmp > 0.0 ? 0.7 : 0.92;
  for (int i = 0; i < uSteps; i++){
    vec3 p = ro + rd * t;
    float d = sdScene(p);
    if (d < 0.00035){ tHit = t; return true; }
    t += max(d * relax, 0.00025);
    if (t > tEnd) break;
  }
  return false;
}

/* ------------------------------------------------------------- environment */

mat3 frameOf(vec3 f){
  vec3 up = abs(f.y) > 0.985 ? vec3(0.0, 0.0, 1.0) : vec3(0.0, 1.0, 0.0);
  vec3 r = normalize(cross(up, f));
  return mat3(r, cross(f, r), f);
}

// rectangular softbox in angular (gnomonic) space
float areaLight(vec3 d, vec3 dir, vec2 size, float soft){
  mat3 F = frameOf(dir);
  float z = dot(d, F[2]);
  if (z <= 0.02) return 0.0;
  vec2 uv = vec2(dot(d, F[0]), dot(d, F[1])) / z;
  vec2 q = abs(uv) - size;
  float dist = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0);
  float s = max(soft, 0.001);
  return 1.0 - smoothstep(-s * 0.35, s, dist);
}

vec3 envMap(vec3 dir, float blur){
  vec3 d = normalize(uEnvRot * dir);
  float y = d.y;

  // piecewise elevation profile: this is what puts the hard tonal bands
  // into the reflection of a near-flat puddle surface
  vec3 c = uGrad[0].yzw;
  for (int i = 1; i < uGradCount; i++){
    vec4 a = uGrad[i - 1];
    vec4 b = uGrad[i];
    float w = max(b.x - a.x, 0.0002) * 0.5;
    c = mix(c, b.yzw, smoothstep(a.x - blur * w * 8.0, b.x + blur * w * 8.0, y));
  }

  // azimuthal panels: a studio is not rotationally uniform, and without this
  // every horizontal blob gets the same perfectly straight racing stripe
  float az = atan(d.x, d.z);
  vec3 w = sin(vec3(az) * uAzFreq + uAzPhase);
  float m = 1.0 + dot(uAzAmp, w) * (1.0 - min(blur * 6.0, 0.7));
  c *= max(m, 0.0);

  for (int i = 0; i < uLightCount; i++){
    vec4 P = uLightPar[i];
    float soft = P.z + blur;
    c += uLightCol[i] * P.w * areaLight(d, uLightDir[i], P.xy + blur * 0.25, soft);
  }
  return c;
}

/* ------------------------------------------------------------------ shading */

vec3 fresnel(vec3 f0, float cosT){
  return f0 + (1.0 - f0) * pow(1.0 - cosT, 5.0);
}

/* ------------------------------------------------------------------- grade */

vec3 aces(vec3 x){
  const float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
  return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

void main(){
  vec3 ro = uCamPos;
  vec3 rd = normalize(vec3(vNdc.x * uAspect * uTanHalf, vNdc.y * uTanHalf, -1.0));

  float t;
  if (!trace(ro, rd, 0.0, t)) discard;

  vec3 p = ro + rd * t;
  vec3 n = calcNormal(p, 0.0006);
  vec3 v = -rd;
  float cosT = clamp(dot(n, v), 0.0, 1.0);
  vec3 F = fresnel(uF0, cosT);
  vec3 r = reflect(rd, n);

  vec3 col;
  if (uBounces > 1){
    float t2;
    vec3 ro2 = p + n * 0.0018;
    if (trace(ro2, r, 0.0, t2)){
      vec3 p2 = ro2 + r * t2;
      vec3 n2 = calcNormal(p2, 0.0006);
      float c2 = clamp(dot(n2, -r), 0.0, 1.0);
      vec3 F2 = fresnel(uF0, c2);
      vec3 r2 = reflect(r, n2);

      vec3 c3 = envMap(r2, uRough * 2.0);
      if (uBounces > 2){
        float t3;
        vec3 ro3 = p2 + n2 * 0.0018;
        if (trace(ro3, r2, 0.0, t3)){
          vec3 p3 = ro3 + r2 * t3;
          vec3 n3 = calcNormal(p3, 0.0008);
          c3 = envMap(reflect(r2, n3), uRough * 4.0) *
               fresnel(uF0, clamp(dot(n3, -r2), 0.0, 1.0)) * 0.9;
        }
      }
      col = c3 * F2 * 0.94;
    } else {
      col = envMap(r, uRough);
    }
  } else {
    col = envMap(r, uRough);
  }
  col *= F;

  col = aces(col * uExposure);
  col = pow(col, vec3(1.0 / 2.2));
  col = (col - 0.5) * uContrast + 0.5;
  float l = dot(col, vec3(0.2126, 0.7152, 0.0722));
  col = clamp(mix(vec3(l), col, uSaturation), 0.0, 1.0);

  fragColor = vec4(col * uOpacity, uOpacity);
}`;

/* ------------------------------------------------------------------ helpers */

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

function catmullRom(pts, samplesPerSeg, tension = 0.5) {
  // pts: [[x,y,z,r], ...] -> densely sampled [[x,y,z,r], ...]
  if (pts.length < 2) return pts.slice();
  const P = pts.map((p) => [p[0], p[1], p[2] || 0, p[3]]);
  const ext = [
    [
      2 * P[0][0] - P[1][0], 2 * P[0][1] - P[1][1],
      2 * P[0][2] - P[1][2], P[0][3],
    ],
    ...P,
    [
      2 * P[P.length - 1][0] - P[P.length - 2][0],
      2 * P[P.length - 1][1] - P[P.length - 2][1],
      2 * P[P.length - 1][2] - P[P.length - 2][2],
      P[P.length - 1][3],
    ],
  ];
  const out = [];
  for (let i = 1; i < ext.length - 2; i++) {
    const p0 = ext[i - 1], p1 = ext[i], p2 = ext[i + 1], p3 = ext[i + 2];
    const steps = samplesPerSeg;
    for (let s = 0; s < steps; s++) {
      const t = s / steps, t2 = t * t, t3 = t2 * t;
      const v = [];
      for (let k = 0; k < 4; k++) {
        const m1 = tension * (p2[k] - p0[k]);
        const m2 = tension * (p3[k] - p1[k]);
        v[k] =
          (2 * t3 - 3 * t2 + 1) * p1[k] +
          (t3 - 2 * t2 + t) * m1 +
          (-2 * t3 + 3 * t2) * p2[k] +
          (t3 - t2) * m2;
      }
      out.push(v);
    }
  }
  out.push([...P[P.length - 1]]);
  return out;
}

/* Walk the dense spline and drop a control point every `factor * localRadius`
   of arc length. Even, radius-proportional spacing is what keeps a mirror
   surface clean: uniform spacing in *absolute* terms leaves dense clusters of
   slightly mismatched cone slants in thin tails, which read as corduroy. */
function resampleByRadius(pts, factor) {
  if (pts.length < 3) return pts.slice();
  const out = [pts[0]];
  let acc = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const a = pts[i - 1], b = pts[i];
    acc += Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
    if (acc >= factor * Math.max(b[3], 1e-6)) { out.push(b); acc = 0; }
  }
  out.push(pts[pts.length - 1]);
  return out;
}

/* Drop points that sit on the chord to within a hair, in both position and
   radius. Curves keep every sample; a straight uniform bar -- a panel, a dock,
   a progress track -- collapses to the single capsule it actually is. */
function prune(pts, tol) {
  if (pts.length < 3) return pts.slice();
  const out = [pts[0]];
  let anchor = 0;
  for (let i = 2; i < pts.length; i++) {
    const a = pts[anchor], b = pts[i];
    const bx = b[0] - a[0], by = b[1] - a[1], bz = b[2] - a[2];
    const len2 = bx * bx + by * by + bz * bz;
    let ok = true;
    for (let j = anchor + 1; j < i; j++) {
      const q = pts[j];
      const qx = q[0] - a[0], qy = q[1] - a[1], qz = q[2] - a[2];
      let t = len2 > 0 ? (qx * bx + qy * by + qz * bz) / len2 : 0;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const dx = qx - bx * t, dy = qy - by * t, dz = qz - bz * t;
      const dev = Math.sqrt(dx * dx + dy * dy + dz * dz);
      const rErr = Math.abs(q[3] - (a[3] + (b[3] - a[3]) * t));
      if (dev + rErr > tol * Math.min(a[3], b[3], q[3])) { ok = false; break; }
    }
    if (!ok) { out.push(pts[i - 1]); anchor = i - 1; }
  }
  out.push(pts[pts.length - 1]);
  return out;
}

function compile(gl, type, src, label) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    throw new Error(`${label} shader:\n${gl.getShaderInfoLog(sh)}`);
  }
  return sh;
}

/* Quality presets. `spacing` (segments per unit radius) and `supersample` are
   the two knobs that actually cost anything; everything else is minor. */
export const QUALITY = {
  ultra:  { supersample: 2.0, spacing: 0.30, bounces: 3, steps: 160 },
  high:   { supersample: 2.0, spacing: 0.40, bounces: 2, steps: 128 },
  medium: { supersample: 1.5, spacing: 0.60, bounces: 2, steps: 104 },
  low:    { supersample: 1.0, spacing: 0.90, bounces: 1, steps: 80 },
};

/* ------------------------------------------------------- default environment */

export const STUDIO = {
  rotation: 0,                       // radians, spins the environment about Y
  // elevation -> radiance. Hard steps here read as hard chrome bands.
  // Elevation -> radiance. The whole look lives here: a bright but NON-clipping
  // upper dome (so bodies stay grey metal rather than blowing out to white),
  // a near-black core band around the horizon, a floor-reflector band, and a
  // hot rim right at the bottom. Blowing highlights is the lights' job.
  gradient: [
    [-1.00, 0.000],                  // straight down: nothing under the puddle
    [-0.86, 0.000],
    [-0.78, 0.700],                  // hot underside rim
    [-0.66, 0.050],
    [-0.48, 0.320],                  // floor reflector -> bright lower flanks
    [-0.30, 0.060],
    [-0.12, 0.010],                  // the black core of every blob
    [ 0.06, 0.028],
    [ 0.20, 0.420],                  // crisp break into the lit upper surface
    [ 0.36, 0.500],
    [ 0.60, 0.720],
    [ 1.00, 1.050],                  // ceiling
  ],
  // brightness modulation by azimuth: amplitude / frequency / phase per harmonic.
  // Without it every horizontal blob gets the same dead-straight racing stripe.
  azimuth: {
    amp:   [0.30, 0.18, 0.10],
    freq:  [2.0, 5.0, 9.0],
    phase: [0.70, -1.90, 2.40],
  },
  // Small, very bright panels -> the blazing streaks over a grey body.
  lights: [
    { dir: [-0.20, 0.94, 0.28], size: [0.30, 0.14], soft: 0.100, intensity: 3.0 },
    { dir: [0.00, 0.99, -0.05], size: [1.20, 0.018], soft: 0.020, intensity: 6.0 },
    { dir: [0.95, 0.18, 0.25], size: [0.05, 0.45], soft: 0.060, intensity: 3.0 },
    { dir: [0.15, -0.32, 0.94], size: [1.00, 0.016], soft: 0.018, intensity: 3.0 },
    { dir: [0.25, 0.50, -0.83], size: [0.22, 0.18], soft: 0.150, intensity: 1.2 },
  ],
};

/* -------------------------------------------------------------- the renderer */

export class LiquidMetal {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', {
      alpha: opts.alpha !== false,
      antialias: false,
      premultipliedAlpha: true,
      preserveDrawingBuffer: !!opts.preserveDrawingBuffer,
      powerPreference: 'high-performance',
    });
    if (!gl) throw new Error('WebGL2 is required for liquid-metal.js');
    this.gl = gl;

    const q = QUALITY[opts.quality || 'high'] || QUALITY.high;
    this.opts = {
      quality: opts.quality || 'high',
      dpr: opts.dpr || Math.min(window.devicePixelRatio || 1, 2),
      supersample: opts.supersample ?? q.supersample,
      maxPixels: opts.maxPixels ?? 12e6,
      fov: opts.fov ?? 18,
      background: opts.background ?? [0, 0, 0, 0],
      exposure: opts.exposure ?? 1.02,
      contrast: opts.contrast ?? 1.14,
      saturation: opts.saturation ?? 1.0,
      roughness: opts.roughness ?? 0.012,
      bounces: opts.bounces ?? q.bounces,
      steps: opts.steps ?? q.steps,
      blend: opts.blend ?? 0.55,
      spacing: opts.spacing ?? q.spacing,
      tint: opts.tint ?? [0.900, 0.905, 0.910],
    };
    this.env = JSON.parse(JSON.stringify(opts.environment || STUDIO));
    this.shapes = [];
    this.time = 0;

    const prog = gl.createProgram();
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT, 'vertex'));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG, 'fragment'));
    gl.bindAttribLocation(prog, 0, 'aPos');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error('link: ' + gl.getProgramInfoLog(prog));
    }
    this.prog = prog;

    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const vbo = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, vbo);
    gl.bufferData(gl.ARRAY_BUFFER,
      new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.vao = vao;

    this.u = {};
    const n = gl.getProgramParameter(prog, gl.ACTIVE_UNIFORMS);
    for (let i = 0; i < n; i++) {
      const info = gl.getActiveUniform(prog, i);
      const name = info.name.replace(/\[0\]$/, '');
      this.u[name] = gl.getUniformLocation(prog, name);
    }

    this._segBuf = new Float32Array(MAX_SEGMENTS * 2 * 4);
    this._grad = new Float32Array(MAX_GRAD * 4);
    this._lightDir = new Float32Array(MAX_LIGHTS * 3);
    this._lightCol = new Float32Array(MAX_LIGHTS * 3);
    this._lightPar = new Float32Array(MAX_LIGHTS * 4);
    this.resize();
  }

  /* ---- sizing ---- */

  resize(cssW, cssH) {
    const c = this.canvas;
    const w = cssW || c.clientWidth || c.width;
    const h = cssH || c.clientHeight || c.height;
    // A canvas is a replaced element: with width:auto its used size is the
    // drawing buffer, even under position:fixed;inset:0. Pin the CSS box when
    // we were given explicit dimensions, or the buffer scale-up silently
    // becomes a display scale-up.
    if (cssW && cssH) {
      c.style.width = w + 'px';
      c.style.height = h + 'px';
    }
    this.cssW = w;
    this.cssH = h;
    let scale = this.opts.dpr * this.opts.supersample;
    while (w * h * scale * scale > this.opts.maxPixels && scale > 0.5) scale *= 0.8;
    c.width = Math.max(1, Math.round(w * scale));
    c.height = Math.max(1, Math.round(h * scale));
    this.aspect = c.width / c.height;

    // world space: canvas height maps to 2 units, +y up, origin at canvas centre
    this.pxToWorld = 2 / h;
    this.tanHalf = Math.tan((this.opts.fov * Math.PI) / 360);
    this.camZ = 1 / this.tanHalf;
    return this;
  }

  /* ---- scene ---- */

  setShapes(list) {
    this.shapes = list.map((s) => this._prepare(s));
    return this;
  }

  addShape(s) {
    const p = this._prepare(s);
    this.shapes.push(p);
    return p;
  }

  clear() { this.shapes = []; return this; }

  /* px (origin top-left, y down) -> world */
  _w(x, y, z) {
    const s = this.pxToWorld;
    return [(x - this.cssW / 2) * s, (this.cssH / 2 - y) * s, (z || 0) * s];
  }

  _prepare(shape) {
    const s = this.pxToWorld;
    const flat = shape.thickness ?? 0.55;
    const blend = shape.blend ?? this.opts.blend;

    // a shape is one or more chains, all smooth-unioned together
    const chains = shape.paths
      ? shape.paths
      : shape.blobs
        ? null
        : [shape.path];

    const asSpheres = !!shape.blobs;
    let groups = [];
    if (asSpheres) {
      groups = [shape.blobs.map((b) => [b[0], b[1], b[2] || 0, b[3]])];
    } else {
      groups = chains.map((ch) => {
        const src = ch.map((p) =>
          p.length === 3 ? [p[0], p[1], 0, p[2]] : [p[0], p[1], p[2], p[3]]);
        return src.length < 2
          ? src
          : catmullRom(src, shape.subdivide ?? 10, shape.tension ?? 0.5);
      });
    }

    // world space, simplify, then hard-cap to the shared segment budget
    const budget = MAX_SEGMENTS;
    const tol = shape.spacing ?? this.opts.spacing;
    groups = groups.map((g) => {
      const W = g.map((p) => {
        const w = this._w(p[0], p[1], p[2]);
        return [w[0], w[1], w[2], p[3] * s];
      });
      return asSpheres || W.length < 3 ? W : prune(resampleByRadius(W, tol), 0.006);
    });
    const totalPts = groups.reduce((n, g) => n + g.length, 0);
    groups = groups.map((g) => {
      let W = g;
      const share = Math.max(2, Math.floor((budget * W.length) / Math.max(totalPts, 1)));
      const maxPts = asSpheres ? share : share + 1;
      if (W.length > maxPts) {
        const step = (W.length - 1) / (maxPts - 1);
        const D = [];
        for (let i = 0; i < maxPts; i++) D.push(W[Math.round(i * step)]);
        W = D;
      }
      return W;
    });
    const W = groups.flat();

    // world AABB, real space. Points carry z already divided by the squash
    // factor, so the real half-extent on z is r * flat.
    const lo = [Infinity, Infinity, Infinity];
    const hi = [-Infinity, -Infinity, -Infinity];
    for (const p of W) {
      const ez = p[3] * flat;
      lo[0] = Math.min(lo[0], p[0] - p[3]); hi[0] = Math.max(hi[0], p[0] + p[3]);
      lo[1] = Math.min(lo[1], p[1] - p[3]); hi[1] = Math.max(hi[1], p[1] + p[3]);
      lo[2] = Math.min(lo[2], p[2] - ez);   hi[2] = Math.max(hi[2], p[2] + ez);
    }
    // the smooth union inflates a little past the primitives
    const pad = (shape.blend ?? this.opts.blend) * 0.35 *
                Math.max(...W.map((p) => p[3])) + 0.002;
    for (let i = 0; i < 3; i++) { lo[i] -= pad; hi[i] += pad; }

    // Bounding sphere for the shader's march range. Fitted to the points, not
    // circumscribed around the AABB -- the box's sphere is up to root-3 larger,
    // and march cost is linear in how long that range is.
    let cx = 0, cy = 0, cz = 0;
    for (const p of W) { cx += p[0]; cy += p[1]; cz += p[2]; }
    cx /= W.length; cy /= W.length; cz /= W.length;
    let br = 0;
    for (const p of W) {
      const d = Math.hypot(p[0] - cx, p[1] - cy, p[2] - cz) + p[3];
      if (d > br) br = d;
    }
    br += pad;
    const bc = [cx, cy, cz];

    // pack segments with Z pre-divided by the squash factor
    const seg = [];
    if (asSpheres) {
      for (const p of W) {
        seg.push([p[0], p[1], p[2] / flat, p[3]], [0, 0, 0, -1]);
      }
    } else {
      for (const g of groups) {
        if (g.length === 1) {
          const p = g[0];
          seg.push([p[0], p[1], p[2] / flat, p[3]], [0, 0, 0, -1]);
          continue;
        }
        for (let i = 0; i < g.length - 1; i++) {
          const a = g[i], b = g[i + 1];
          const dx = a[0] - b[0], dy = a[1] - b[1], dz = (a[2] - b[2]) / flat;
          if (dx * dx + dy * dy + dz * dz < 1e-12) continue;
          if (seg.length >= MAX_SEGMENTS * 2) break;
          seg.push([a[0], a[1], a[2] / flat, a[3]], [b[0], b[1], b[2] / flat, b[3]]);
        }
      }
    }

    return {
      src: shape,
      seg,
      segCount: seg.length / 2,
      flat,
      smooth: blend,
      lo, hi,
      bound: [bc[0], bc[1], bc[2], br],
      tint: shape.tint || this.opts.tint,
      opacity: shape.opacity ?? 1,
      roughness: shape.roughness ?? this.opts.roughness,
      bounces: shape.bounces ?? this.opts.bounces,
      steps: shape.steps ?? this.opts.steps,
      envRotation: shape.envRotation ?? null,
      wobble: shape.wobble || null,
    };
  }

  /* ---- environment ---- */

  setEnvironment(env) {
    this.env = { ...this.env, ...env };
    return this;
  }

  _envMatrix(extraY) {
    const a = (this.env.rotation || 0) + (extraY || 0);
    const c = Math.cos(a), s = Math.sin(a);
    // column-major rotation about Y
    return new Float32Array([c, 0, -s, 0, 1, 0, s, 0, c]);
  }

  /* ---- draw ---- */

  render(time) {
    const gl = this.gl;
    if (time !== undefined) this.time = time;

    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    const bg = this.opts.background;
    gl.clearColor(bg[0] * bg[3], bg[1] * bg[3], bg[2] * bg[3], bg[3]);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.disable(gl.DEPTH_TEST);

    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);

    const u = this.u;
    gl.uniform3f(u.uCamPos, 0, 0, this.camZ);
    gl.uniform1f(u.uTanHalf, this.tanHalf);
    gl.uniform1f(u.uAspect, this.aspect);
    gl.uniform1f(u.uExposure, this.opts.exposure);
    gl.uniform1f(u.uContrast, this.opts.contrast);
    gl.uniform1f(u.uSaturation, this.opts.saturation);
    gl.uniform1f(u.uTime, this.time);

    // environment
    const e = this.env;
    const grad = e.gradient.slice(0, MAX_GRAD);
    grad.forEach((g, i) => {
      const c = typeof g[1] === 'number' ? [g[1], g[1], g[1]] : g[1];
      this._grad[i * 4] = g[0];
      this._grad[i * 4 + 1] = c[0];
      this._grad[i * 4 + 2] = c[1];
      this._grad[i * 4 + 3] = c[2];
    });
    gl.uniform4fv(u.uGrad, this._grad);
    gl.uniform1i(u.uGradCount, grad.length);
    const az = e.azimuth || { amp: [0, 0, 0], freq: [1, 1, 1], phase: [0, 0, 0] };
    gl.uniform3fv(u.uAzAmp, az.amp);
    gl.uniform3fv(u.uAzFreq, az.freq);
    gl.uniform3fv(u.uAzPhase, az.phase);
    const lights = e.lights.slice(0, MAX_LIGHTS);
    lights.forEach((L, i) => {
      const d = L.dir, n = Math.hypot(d[0], d[1], d[2]) || 1;
      this._lightDir[i * 3] = d[0] / n;
      this._lightDir[i * 3 + 1] = d[1] / n;
      this._lightDir[i * 3 + 2] = d[2] / n;
      const c = L.color || [1, 1, 1];
      this._lightCol[i * 3] = c[0];
      this._lightCol[i * 3 + 1] = c[1];
      this._lightCol[i * 3 + 2] = c[2];
      this._lightPar[i * 4] = L.size[0];
      this._lightPar[i * 4 + 1] = L.size[1];
      this._lightPar[i * 4 + 2] = L.soft;
      this._lightPar[i * 4 + 3] = L.intensity;
    });
    gl.uniform3fv(u.uLightDir, this._lightDir);
    gl.uniform3fv(u.uLightCol, this._lightCol);
    gl.uniform4fv(u.uLightPar, this._lightPar);
    gl.uniform1i(u.uLightCount, lights.length);

    for (const sh of this.shapes) {
      if (!sh.segCount) continue;
      const rect = this._screenRect(sh);
      if (!rect) continue;

      for (let i = 0; i < sh.seg.length; i++) {
        this._segBuf[i * 4] = sh.seg[i][0];
        this._segBuf[i * 4 + 1] = sh.seg[i][1];
        this._segBuf[i * 4 + 2] = sh.seg[i][2];
        this._segBuf[i * 4 + 3] = sh.seg[i][3];
      }
      gl.uniform4fv(u.uSeg, this._segBuf);
      gl.uniform1i(u.uSegCount, sh.segCount);
      gl.uniform1f(u.uFlat, sh.flat);
      gl.uniform1f(u.uSmooth, sh.smooth);
      gl.uniform4fv(u.uBound, sh.bound);
      gl.uniform3fv(u.uF0, sh.tint);
      gl.uniform1f(u.uRough, sh.roughness);
      gl.uniform1i(u.uBounces, sh.bounces);
      gl.uniform1f(u.uOpacity, sh.opacity);
      gl.uniform1i(u.uSteps, sh.steps);
      gl.uniformMatrix3fv(u.uEnvRot, false, this._envMatrix(sh.envRotation));
      gl.uniform1f(u.uWobbleAmp, sh.wobble ? sh.wobble.amp * this.pxToWorld : 0);
      gl.uniform1f(u.uWobbleFreq, sh.wobble ? sh.wobble.freq / this.pxToWorld * 0.001 : 0);
      gl.uniform4f(u.uRect, rect[0], rect[1], rect[2], rect[3]);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  }

  // NDC rect covering the shape's projected AABB
  _screenRect(sh) {
    const { lo, hi } = sh;
    let x0 = 1, y0 = 1, x1 = -1, y1 = -1;
    for (let i = 0; i < 8; i++) {
      const x = i & 1 ? hi[0] : lo[0];
      const y = i & 2 ? hi[1] : lo[1];
      const z = i & 4 ? hi[2] : lo[2];
      const dz = this.camZ - z;
      if (dz <= 1e-4) return [-1, -1, 1, 1];      // straddles the camera plane
      const k = 1 / (dz * this.tanHalf);
      const px = (x * k) / this.aspect;
      const py = y * k;
      if (px < x0) x0 = px;
      if (px > x1) x1 = px;
      if (py < y0) y0 = py;
      if (py > y1) y1 = py;
    }
    const m = 0.004;
    x0 = clamp(x0 - m, -1, 1); x1 = clamp(x1 + m, -1, 1);
    y0 = clamp(y0 - m, -1, 1); y1 = clamp(y1 + m, -1, 1);
    if (x1 <= x0 || y1 <= y0) return null;
    return [x0, y0, x1, y1];
  }

  /* Which shape is under a CSS-pixel point, or null. Evaluates the same field
     the shader does, flattened to 2D — enough to hit-test dock icons and other
     blob-shaped controls. Topmost (last drawn) wins. */
  shapeAt(px, py) {
    const w = this._w(px, py, 0);
    for (let i = this.shapes.length - 1; i >= 0; i--) {
      if (this._distance2D(this.shapes[i], w[0], w[1]) <= 0) return this.shapes[i];
    }
    return null;
  }

  /* Signed distance in world units from a point to a shape, in the XY plane. */
  _distance2D(sh, x, y) {
    let d = 1e9;
    for (let i = 0; i < sh.segCount; i++) {
      const A = sh.seg[i * 2], B = sh.seg[i * 2 + 1];
      let di, r;
      if (B[3] < 0) {
        di = Math.hypot(x - A[0], y - A[1]) - A[3];
        r = A[3];
      } else {
        const bax = B[0] - A[0], bay = B[1] - A[1];
        const pax = x - A[0], pay = y - A[1];
        const len2 = bax * bax + bay * bay;
        let t = len2 > 0 ? (pax * bax + pay * bay) / len2 : 0;
        t = t < 0 ? 0 : t > 1 ? 1 : t;
        di = Math.hypot(pax - bax * t, pay - bay * t) - (A[3] + (B[3] - A[3]) * t);
        r = Math.min(A[3], B[3]);
      }
      const k = sh.smooth * r;
      if (k > 0) {
        const h = Math.max(k - Math.abs(d - di), 0) / k;
        d = Math.min(d, di) - h * h * h * k * (1 / 6);
      } else {
        d = Math.min(d, di);
      }
    }
    return d;
  }

  /* World-space signed distance in CSS pixels, for softer proximity effects. */
  distanceAt(px, py, shape) {
    const w = this._w(px, py, 0);
    const list = shape ? [shape] : this.shapes;
    let best = Infinity;
    for (const sh of list) best = Math.min(best, this._distance2D(sh, w[0], w[1]));
    return best / this.pxToWorld;
  }

  /* Swap quality preset at runtime; shapes are re-prepared at the new spacing. */
  setQuality(name) {
    const q = QUALITY[name];
    if (!q) throw new Error('unknown quality: ' + name);
    Object.assign(this.opts, { quality: name }, q);
    this.resize(this.cssW, this.cssH);
    this.setShapes(this.shapes.map((s) => s.src));
    return this;
  }

  start(update) {
    const loop = (ms) => {
      this._raf = requestAnimationFrame(loop);
      const t = ms / 1000;
      if (update) update(t, this);
      this.render(t);
    };
    this._raf = requestAnimationFrame(loop);
    return this;
  }

  stop() { cancelAnimationFrame(this._raf); return this; }
}

LiquidMetal.STUDIO = STUDIO;
LiquidMetal.QUALITY = QUALITY;
if (typeof globalThis !== 'undefined') {
  globalThis.LiquidMetal = LiquidMetal;
}
export default LiquidMetal;
