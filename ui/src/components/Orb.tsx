"use client";

import { useEffect, useRef, type RefObject } from "react";
import type { LevelSample, Presence } from "@/lib/useNieeeki";

/**
 * Lebendige Praesenz von nieeeki: ein gluehender Kern (WebGL-Shader), gefasst von HudOrb.
 * - schlaeft: gedimmt, langsamer Atem
 * - ist da: ruhiges Atmen, Glut fliesst zwischen Rotorange und Orange
 * - hoert zu: neigt sich zur Farbe des Sprechers (Nikolaj warmes Weiss), Rand zittert mit seiner Stimme
 * - denkt nach: schneller Wirbel mit umlaufendem Lichtbogen
 * - spricht: Rotorange, pulsiert mit nieekis Stimme
 * - handelt (Werkzeug laeuft): kreisende Lichtpunkte
 * Pegel werden pro Bild aus einem Ref gelesen, React rendert dafuer nicht neu.
 */

const SPEAKER_RGB = [[241, 237, 230], [255, 176, 32], [110, 200, 160], [236, 112, 170], [120, 170, 255]];
const BOT_RGB = [255, 74, 28];

const STATE: Record<Presence, { awake: number; think: number; speed: number }> = {
  offline: { awake: 0.42, think: 0, speed: 0.35 },
  connecting: { awake: 0.75, think: 0.7, speed: 1 },
  idle: { awake: 1, think: 0, speed: 0.55 },
  listening: { awake: 1, think: 0, speed: 0.8 },
  thinking: { awake: 1, think: 1, speed: 1.4 },
  speaking: { awake: 1, think: 0, speed: 1 },
};

const VERT = "attribute vec2 aPos; void main(){ gl_Position = vec4(aPos, 0.0, 1.0); }";

const FRAG = `
precision mediump float;
uniform vec2 uRes;
uniform float uTime, uUser, uBot, uBreath, uThink, uAwake, uAct;
uniform vec3 uUserCol, uBotCol;

float hash(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
float noise(vec3 x) {
  vec3 i = floor(x); vec3 f = fract(x); f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
    mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
float fbm(vec3 p) { float v = 0.0; float a = 0.5; for (int i = 0; i < 4; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; } return v; }

void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y);
  float r = length(uv);
  float a = atan(uv.y, uv.x);
  float speak = clamp(max(uUser, uBot), 0.0, 1.0);
  float t = uTime;

  // Rand: atmet, waechst mit der Stimme, wird vom Rauschen gewellt (staerker beim Sprechen/Denken)
  float n = fbm(vec3(cos(a) * 1.6 + t * 0.25, sin(a) * 1.6 - t * 0.2, t * 0.45));
  // kleiner als die HUD-Fassung (r 0,295): dunkler Spalt zwischen Glut und Ring
  float radius = 0.16 + 0.012 * uBreath + 0.05 * speak + (n - 0.5) * (0.02 + 0.06 * speak + 0.035 * uThink);
  float inside = smoothstep(radius, radius - 0.01, r);

  // Inneres: fliessende Tiefe, beim Denken als Wirbel
  float swirl = uThink * 1.8 * (0.35 - r);
  vec2 w = vec2(cos(swirl) * uv.x - sin(swirl) * uv.y, sin(swirl) * uv.x + cos(swirl) * uv.y);
  float m = fbm(vec3(w * 3.2 + vec2(t * 0.18, -t * 0.12), t * 0.35));
  float userShare = uUser / (uUser + uBot + 0.0001);
  vec3 voice = mix(uBotCol, uUserCol, userShare * 0.55);  // Nikolaj hellt auf, die Glut bleibt warm
  vec3 idle = mix(uBotCol, vec3(1.0, 0.54, 0.16), 0.3 + 0.25 * sin(t * 0.4));  // Glut: Rotorange <-> Orange
  vec3 col = mix(idle, voice, smoothstep(0.02, 0.18, speak));
  col = mix(col * 0.5, col * 1.2, m);
  col += vec3(1.0, 0.72, 0.32) * 0.75 * pow(max(0.0, 1.0 - r / max(radius, 0.001)), 2.2);   // heisse Mitte

  // Lichtkomet beim Denken (Kopf voraus, Schweif dahinter, leise Umlaufbahn),
  // kreisende Lichtpunkte mit Hof beim Handeln
  float ring = smoothstep(0.04, 0.0, abs(r - radius - 0.04));
  float behind = mod(t * 2.6 - a, 6.2831853);
  float arc = (0.9 * exp(-behind * 1.6) + 0.9 * exp(-(6.2831853 - behind) * 14.0) + 0.08) * ring * uThink;
  float dots = 0.0;
  for (int k = 0; k < 3; k++) {
    float ang = t * 1.4 + float(k) * 2.094;
    float d = length(uv - vec2(cos(ang), sin(ang)) * (radius + 0.07));
    dots += smoothstep(0.02, 0.0, d) + 0.35 * exp(-d * 45.0);
  }
  dots *= uAct;

  // Leuchten nach aussen
  float glow = exp(-max(r - radius, 0.0) * 11.0) * (0.32 + 0.5 * speak + 0.2 * uThink);
  vec3 halo = mix(uBotCol, voice, 0.6);
  float alpha = inside + (1.0 - inside) * glow;
  vec3 outCol = mix(halo, col, inside);
  outCol += (arc + dots) * mix(vec3(1.0), halo, 0.35);
  alpha = clamp(alpha + arc + dots, 0.0, 1.0) * uAwake;
  gl_FragColor = vec4(outCol * alpha, alpha);
}`;

function rgb(c: number[]) {
  return c.map((v) => v / 255);
}

export function Orb({
  presence,
  levels,
  acting,
}: {
  presence: Presence;
  levels: RefObject<LevelSample>;
  acting: boolean;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef({ presence, acting });

  useEffect(() => {
    stateRef.current = { presence, acting };
  }, [presence, acting]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const gl = canvas?.getContext("webgl", { alpha: true, premultipliedAlpha: true, antialias: true });
    if (!canvas || !gl) return;

    const shader = (type: number, src: string) => {
      const s = gl.createShader(type)!;
      gl.shaderSource(s, src);
      gl.compileShader(s);
      return s;
    };
    const prog = gl.createProgram()!;
    gl.attachShader(prog, shader(gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, shader(gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
    gl.useProgram(prog);
    gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);
    const u = (name: string) => gl.getUniformLocation(prog, name);
    gl.uniform3fv(u("uBotCol"), rgb(BOT_RGB));

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const smooth = { user: 0, bot: 0, awake: 0.4, think: 0, speed: 0.4, act: 0 };
    let raf = 0;
    let clock = 0;
    let last = performance.now();

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.round(canvas.clientWidth * dpr);
      canvas.height = Math.round(canvas.clientHeight * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(canvas);
    resize();

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const { presence: p, acting: act } = stateRef.current;
      const target = STATE[p];
      const lv = levels.current;
      const ease = (k: keyof typeof smooth, to: number, rate: number) => {
        smooth[k] += (to - smooth[k]) * Math.min(1, rate * dt);
      };
      const user = Math.min(1, (lv?.user ?? 0) * 3.2);
      const bot = Math.min(1, (lv?.bot ?? 0) * 3.2);
      ease("user", user, user > smooth.user ? 18 : 4);
      ease("bot", bot, bot > smooth.bot ? 18 : 4);
      ease("awake", target.awake, 3);
      ease("think", target.think, 3);
      ease("speed", target.speed, 2);
      ease("act", act ? 1 : 0, 4);
      clock += dt * smooth.speed * (reduced ? 0.25 : 1);
      const breath = reduced ? 0.5 : 0.5 + 0.5 * Math.sin((now / (p === "offline" ? 8000 : 5000)) * Math.PI * 2);

      gl.uniform2f(u("uRes"), canvas.width, canvas.height);
      gl.uniform1f(u("uTime"), clock);
      gl.uniform1f(u("uUser"), smooth.user);
      gl.uniform1f(u("uBot"), smooth.bot);
      gl.uniform1f(u("uBreath"), breath);
      gl.uniform1f(u("uThink"), smooth.think);
      gl.uniform1f(u("uAwake"), smooth.awake);
      gl.uniform1f(u("uAct"), smooth.act);
      gl.uniform3fv(u("uUserCol"), rgb(SPEAKER_RGB[(lv?.speaker ?? 0) % SPEAKER_RGB.length]));
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [levels]);

  return <canvas ref={canvasRef} className="orb" aria-hidden="true" data-presence={presence} />;
}
