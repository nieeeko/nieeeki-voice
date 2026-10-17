// Welliger Stimmen-Rand: zeichnet entlang aller vier Bildschirmkanten einen weichen, farbigen Saum.
// Nikolaj = seeblau, nieeeki = indigo. Spricht niemand, bleibt nur ein Hauch sichtbar.
// Weitere Sprecher bekommen spaeter eigene Farben (Sprechererkennung im Voice-Core).

const VOICES = [
  { key: "user", rgb: [79, 179, 199], phase: 0 },
  { key: "bot", rgb: [154, 134, 242], phase: Math.PI },
];
const IDLE_ALPHA = 0.06;
const MAX_DEPTH = 26; // px, wie weit der Saum bei voller Lautstaerke hineinreicht

const canvas = document.getElementById("edge");
const ctx = canvas.getContext("2d");
const level = { user: 0, bot: 0 };
// Farben je Sprecher: 0 = Nikolaj (seeblau), dann weitere Stimmen im Raum
const SPEAKER_RGB = [[79, 179, 199], [242, 170, 76], [110, 200, 120], [236, 112, 170], [230, 210, 90]];
let speaker = 0;
const target = { user: 0, bot: 0 };
let cutFlash = 0;

window.overlay.onLevels((s) => {
  target.user = s.user;
  target.bot = s.bot;
  speaker = s.speaker || 0;
  if (s.cut) cutFlash = 1;
});

function resize() {
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(window.innerWidth * dpr);
  canvas.height = Math.round(window.innerHeight * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
window.addEventListener("resize", resize);
resize();

// Punkte entlang des Bildschirmrands (im Uhrzeigersinn), mit Normalen nach innen
function edgePoints(w, h, step) {
  const pts = [];
  for (let x = 0; x <= w; x += step) pts.push([x, 0, 0, 1]);
  for (let y = 0; y <= h; y += step) pts.push([w, y, -1, 0]);
  for (let x = w; x >= 0; x -= step) pts.push([x, h, 0, -1]);
  for (let y = h; y >= 0; y -= step) pts.push([0, y, 1, 0]);
  return pts;
}

function draw(t) {
  const w = window.innerWidth;
  const h = window.innerHeight;
  ctx.clearRect(0, 0, w, h);

  // weich nachziehen, damit der Rand atmet statt zu flackern
  for (const k of ["user", "bot"]) level[k] += (target[k] - level[k]) * (target[k] > level[k] ? 0.35 : 0.08);
  cutFlash *= 0.92;

  const pts = edgePoints(w, h, 14);
  const perimeter = 2 * (w + h);

  for (const v of VOICES) {
    const a = Math.min(1, level[v.key] * 3.2); // Pegel kommen eher leise an
    const depth = 3 + a * MAX_DEPTH;
    const alpha = IDLE_ALPHA + a * 0.75;
    const [r, g, b] = v.key === "user" ? SPEAKER_RGB[speaker % SPEAKER_RGB.length] : v.rgb;

    ctx.beginPath();
    pts.forEach(([x, y, nx, ny], i) => {
      const s = (i * 14) / perimeter;
      const wave =
        Math.sin(s * Math.PI * 18 + t / 260 + v.phase) * 0.5 +
        Math.sin(s * Math.PI * 7 - t / 410 + v.phase) * 0.5;
      const d = depth * (0.65 + 0.35 * wave);
      const px = x + nx * d;
      const py = y + ny * d;
      i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.closePath();

    ctx.lineWidth = 2 + a * 6;
    ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
    ctx.shadowColor = `rgba(${r},${g},${b},${Math.min(1, alpha + 0.2)})`;
    ctx.shadowBlur = 8 + a * 30;
    ctx.stroke();
  }

  if (cutFlash > 0.02) {
    // kurzer roter Hauch, wenn Nikolaj nieeeki unterbricht
    ctx.lineWidth = 6;
    ctx.shadowBlur = 24;
    ctx.strokeStyle = `rgba(240,122,110,${cutFlash * 0.8})`;
    ctx.shadowColor = ctx.strokeStyle;
    ctx.strokeRect(3, 3, w - 6, h - 6);
  }
  requestAnimationFrame(draw);
}
requestAnimationFrame(draw);
