// Stimmen-Schleier: ein weicher, fliessender Saum entlang aller Bildschirmkanten, der nach innen
// transparent auslaeuft (Standard 4,55 cm; NIEEEKI_VEIL_CM in der Desktop-App). Leuchtet bei jeder
// Stimme: Nikolaj und weitere Sprecher ueber das Mikrofon, nieeeki ueber ihren gemessenen Pegel.
//
// WebGL-Fragment-Shader: jeder Bildpunkt kennt seinen Abstand zur naechsten Kante. Im Leerlauf
// "atmet" der Schleier leise (6-s-Zyklus), beim Sprechen waechst und leuchtet er in der Farbe der
// Stimme: Nikolaj seeblau, weitere Sprecher eigene Farben, nieeeki indigo, Unterbrechung rot.
// Leistung: halbe Aufloesung (der Schleier ist ohnehin weich), Mitte wird sofort verworfen,
// 30 Bilder/s beim Sprechen, 12 im Leerlauf, weniger Bewegung bei prefers-reduced-motion.

const SPEAKER_RGB = [[79, 179, 199], [242, 170, 76], [110, 200, 120], [236, 112, 170], [230, 210, 90]];
const BOT_RGB = [154, 134, 242];
const RENDER_SCALE = 0.5;
const VEIL_DIP = Number(new URLSearchParams(location.search).get("veil")) || 172;
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const level = { user: 0, bot: 0 };
const target = { user: 0, bot: 0 };
let speaker = 0;
let cut = 0;
let wake = 0; // Weckwort gehoert: kurz in nieekis Indigo aufleuchten, bis ihr "Ja?" kommt

window.overlay.onLevels((s) => {
  target.user = s.user;
  target.bot = s.bot;
  speaker = s.speaker || 0;
  if (s.cut) cut = 1;
});
window.overlay.onWake(() => {
  wake = 1;
});

const VERT = `
attribute vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }`;

const FRAG = `
precision mediump float;
uniform vec2 uRes;
uniform float uTime, uVeil, uUser, uBot, uCut, uBreath;
uniform vec3 uUserCol, uBotCol;

float hash(vec3 p) {
  p = fract(p * 0.3183099 + 0.1);
  p *= 17.0;
  return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
}
float noise(vec3 x) {
  vec3 i = floor(x);
  vec3 f = fract(x);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(mix(hash(i), hash(i + vec3(1, 0, 0)), f.x), mix(hash(i + vec3(0, 1, 0)), hash(i + vec3(1, 1, 0)), f.x), f.y),
    mix(mix(hash(i + vec3(0, 0, 1)), hash(i + vec3(1, 0, 1)), f.x), mix(hash(i + vec3(0, 1, 1)), hash(i + vec3(1, 1, 1)), f.x), f.y),
    f.z);
}
float fbm(vec3 p) {
  float v = 0.0;
  float a = 0.5;
  for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; }
  return v;
}

void main() {
  vec2 p = gl_FragCoord.xy;
  float dx = min(p.x, uRes.x - p.x);
  float dy = min(p.y, uRes.y - p.y);
  if (min(dx, dy) > uVeil * 1.6) { gl_FragColor = vec4(0.0); return; }  // Mitte bleibt frei
  // weiches Minimum statt min(): runde Ecken ohne diagonale Naht
  float k = uVeil * 0.35;
  float d = -k * log(exp(-dx / k) + exp(-dy / k));

  float speak = clamp(max(uUser, uBot), 0.0, 1.0);
  // fliessendes, organisches Muster in Bildschirmkoordinaten, schneller beim Sprechen
  vec3 q = vec3(p / (uVeil * 1.3), uTime * (0.10 + 0.35 * speak));
  float n = fbm(q);

  // Breite: atmet im Leerlauf, waechst mit der Stimme, wird vom Muster gewellt
  float w = uVeil * (0.62 + 0.10 * uBreath + 0.38 * speak) * (0.72 + 0.56 * n);
  float f = 1.0 - smoothstep(0.0, w, d);
  f = f * f * (3.0 - 2.0 * f);  // weicher Schleier nach innen

  float total = uUser + uBot + 0.0001;
  vec3 voice = (uUserCol * uUser + uBotCol * uBot) / total;
  vec3 col = mix(uBotCol * 0.9, voice, smoothstep(0.02, 0.2, speak));
  col += 0.18 * (n - 0.5);                         // Schimmer im Schleier
  col = mix(col, vec3(0.94, 0.48, 0.43), uCut * 0.75);

  float alpha = f * (0.07 + 0.06 * uBreath + 0.55 * speak + 0.45 * uCut);
  alpha *= 0.82 + 0.36 * n;
  gl_FragColor = vec4(col * alpha, alpha);         // vormultipliziertes Alpha
}`;

const canvas = document.getElementById("edge");
const gl = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: true, antialias: false });

function compile(type, src) {
  const s = gl.createShader(type);
  gl.shaderSource(s, src);
  gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
  return s;
}

function rgb(c) {
  return c.map((v) => v / 255);
}

if (!gl) {
  console.warn("WebGL nicht verfuegbar: Stimmen-Schleier aus");
} else {
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl.VERTEX_SHADER, VERT));
  gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, FRAG));
  gl.linkProgram(prog);
  gl.useProgram(prog);

  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, "aPos");
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  const u = {};
  for (const name of ["uRes", "uTime", "uVeil", "uUser", "uBot", "uCut", "uBreath", "uUserCol", "uBotCol"]) {
    u[name] = gl.getUniformLocation(prog, name);
  }
  gl.uniform3fv(u.uBotCol, rgb(BOT_RGB));

  let scale = 1;
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    scale = dpr * RENDER_SCALE;
    canvas.width = Math.round(window.innerWidth * scale);
    canvas.height = Math.round(window.innerHeight * scale);
    gl.viewport(0, 0, canvas.width, canvas.height);
  }
  window.addEventListener("resize", resize);
  resize();

  let last = 0;
  function frame(now) {
    requestAnimationFrame(frame);
    for (const k of ["user", "bot"]) {
      const t = Math.min(1, target[k] * 3.2);  // Pegel kommen eher leise an
      level[k] += (t - level[k]) * (t > level[k] ? 0.3 : 0.06);
    }
    cut *= 0.93;
    wake *= 0.96; // nach ~1,5 s verklungen
    const bot = Math.max(level.bot, wake);
    const speaking = Math.max(level.user, bot) > 0.03 || cut > 0.02;
    const interval = 1000 / (speaking ? 30 : 12);
    if (now - last < interval) return;
    last = now;

    const t = (now / 1000) * (reducedMotion ? 0.3 : 1);
    const breath = reducedMotion ? 0.5 : 0.5 + 0.5 * Math.sin((now / 6000) * Math.PI * 2);
    gl.uniform2f(u.uRes, canvas.width, canvas.height);
    gl.uniform1f(u.uTime, t);
    gl.uniform1f(u.uVeil, VEIL_DIP * scale);
    gl.uniform1f(u.uUser, level.user);
    gl.uniform1f(u.uBot, bot);
    gl.uniform1f(u.uCut, cut);
    gl.uniform1f(u.uBreath, breath);
    gl.uniform3fv(u.uUserCol, rgb(SPEAKER_RGB[speaker % SPEAKER_RGB.length]));
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
  requestAnimationFrame(frame);
}

// Lichtrahmen: wo nieeeki gerade handelt. main.js schickt Rahmen und Klickpunkt schon in den
// Koordinaten dieses Monitors (geraeteunabhaengige Pixel); rot, wenn die Aktion abgelehnt wurde.
function flash(kind, box, ok, ms) {
  const el = document.createElement("div");
  el.className = ok ? kind : `${kind} nein`;
  for (const [key, value] of Object.entries(box)) el.style[key] = `${Math.round(value)}px`;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

window.overlay.onHighlight((h) => {
  if (h.rahmen) flash("frame", { left: h.rahmen.x, top: h.rahmen.y, width: h.rahmen.w, height: h.rahmen.h }, h.ok, 1800);
  if (h.punkt) flash("tap", { left: h.punkt.x, top: h.punkt.y }, h.ok, 1000);
});
