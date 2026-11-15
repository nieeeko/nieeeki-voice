// Tests fuer csp.ts: Stufen, Inhalt der Policy, Header je Stufe und die Bereinigung der Browser-Meldungen.
// Ausfuehren: npm test   (im Ordner ui/)
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BASELINE_POLICY,
  MAX_REPORT_CHARS,
  REPORT_PATH,
  cspHeaders,
  cspMode,
  cspModeIsKnown,
  httpOrigin,
  newNonce,
  parseCspReports,
  readLimitedText,
  requestHeadersForNext,
  securityHeaders,
  strictPolicy,
} from "./csp.ts";

test("Tippfehler in NIEEEKI_CSP wird erkannt (Proxy warnt, es gilt beobachten)", () => {
  for (const raw of [undefined, "", " Streng ", "aus", "beobachten"]) assert.equal(cspModeIsKnown(raw), true);
  for (const raw of ["strict", "an", "1", "strenge"]) assert.equal(cspModeIsKnown(raw), false);
});

test("Proxy: Policy und Nonce des Clients werden nie an Next weitergereicht", () => {
  const fremd = new Headers({
    "content-security-policy": "script-src 'nonce-ANGREIFER'",
    "content-security-policy-report-only": "script-src 'nonce-ANGREIFER2'",
    "next-router-prefetch": "1",
    authorization: "Bearer x",
  });
  const eigen = strictPolicy(INPUT);
  const mitPolicy = requestHeadersForNext(fremd, eigen);
  assert.equal(mitPolicy.get("content-security-policy"), eigen);
  assert.equal(mitPolicy.get("content-security-policy-report-only"), null);
  assert.equal(mitPolicy.get("authorization"), "Bearer x"); // alles andere bleibt
  assert.equal(mitPolicy.get("next-router-prefetch"), "1");

  const ohnePolicy = requestHeadersForNext(fremd, null); // Stufe "aus"
  assert.equal(ohnePolicy.get("content-security-policy"), null);
  assert.equal(ohnePolicy.get("content-security-policy-report-only"), null);
  assert.equal(fremd.get("content-security-policy"), "script-src 'nonce-ANGREIFER'"); // Eingabe unveraendert
});

test("Berichts-Body: nur bis zur Grenze gelesen, auch ohne Content-Length", async () => {
  const stream = (...parts: string[]) =>
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const part of parts) controller.enqueue(new TextEncoder().encode(part));
        controller.close();
      },
    });
  assert.equal(await readLimitedText(null, 10), "");
  assert.equal(await readLimitedText(stream('{"a":', '"ä"}'), 100), '{"a":"ä"}');
  assert.equal(await readLimitedText(stream("x".repeat(60), "y".repeat(60)), 100), null);
  // ein Teil, der die Grenze reisst, bricht ab, bevor weitere gelesen werden
  let pulled = 0;
  const endlos = new ReadableStream<Uint8Array>({
    pull(controller) {
      pulled += 1;
      controller.enqueue(new Uint8Array(64));
    },
  });
  assert.equal(await readLimitedText(endlos, 200), null);
  assert.ok(pulled < 10, `zu viel gelesen: ${pulled}`);
});

const INPUT = {
  nonce: "Tm9uY2VOb25jZU5vbmNlTm9u",
  supabaseUrl: "https://abc.supabase.co/",
  voiceCoreUrl: "https://laptop.tailnet.ts.net:7860/pfad",
};

function directives(policy: string): Map<string, string> {
  return new Map(
    policy.split("; ").map((part) => {
      const space = part.indexOf(" ");
      return space < 0 ? [part, ""] : [part.slice(0, space), part.slice(space + 1)];
    }),
  );
}

test("Stufe: Standard ist beobachten, in der Entwicklung immer aus", () => {
  assert.equal(cspMode(undefined, true), "beobachten");
  assert.equal(cspMode("", true), "beobachten");
  assert.equal(cspMode("quatsch", true), "beobachten");
  assert.equal(cspMode(" Streng ", true), "streng");
  assert.equal(cspMode("aus", true), "aus");
  for (const raw of [undefined, "streng", "beobachten", "aus"]) assert.equal(cspMode(raw, false), "aus");
});

test("httpOrigin nimmt nur http(s) und liefert nur die Herkunft", () => {
  assert.equal(httpOrigin("https://abc.supabase.co/rest/v1?x=1"), "https://abc.supabase.co");
  assert.equal(httpOrigin("http://localhost:7860"), "http://localhost:7860");
  for (const raw of [undefined, "", "kein url", "javascript:alert(1)", "data:text/html,x", "ftp://x"]) {
    assert.equal(httpOrigin(raw), null);
  }
});

test("Nonce: frisch je Aufruf und ohne Zeichen, die einen Header oder ein Attribut brechen", () => {
  const seen = new Set(Array.from({ length: 50 }, () => newNonce()));
  assert.equal(seen.size, 50);
  for (const nonce of seen) assert.match(nonce, /^[A-Za-z0-9+/]{24}$/);
});

test("strenge Policy: Skripte nur mit Nonce, kein eval, keine Inline-Skripte", () => {
  const d = directives(strictPolicy(INPUT));
  assert.equal(d.get("script-src"), `'self' 'nonce-${INPUT.nonce}' 'strict-dynamic'`);
  assert.equal(d.get("default-src"), "'self'");
  assert.equal(d.get("object-src"), "'none'");
  assert.equal(d.get("base-uri"), "'none'");
  assert.equal(d.get("frame-ancestors"), "'none'");
  assert.equal(d.get("form-action"), "'self'");
  assert.equal(d.get("report-uri"), REPORT_PATH);
  const script = d.get("script-src") ?? "";
  for (const forbidden of ["unsafe-eval", "unsafe-inline", "https:", "*", "data:", "blob:"]) {
    assert.ok(!script.includes(forbidden), `script-src enthaelt ${forbidden}`);
  }
});

test("strenge Policy: Verbindungen nur zu sich selbst, Supabase (https und wss) und dem Voice-Core", () => {
  const d = directives(strictPolicy(INPUT));
  assert.equal(
    d.get("connect-src"),
    "'self' https://abc.supabase.co wss://abc.supabase.co https://laptop.tailnet.ts.net:7860",
  );
  // ohne oder mit kaputten Adressen bleibt nur 'self'
  const leer = directives(strictPolicy({ nonce: INPUT.nonce, supabaseUrl: "kaputt", voiceCoreUrl: undefined }));
  assert.equal(leer.get("connect-src"), "'self'");
  // http wird zu ws (lokaler Voice-Core / lokales Supabase)
  const lokal = directives(strictPolicy({ nonce: INPUT.nonce, supabaseUrl: "http://127.0.0.1:54321" }));
  assert.equal(lokal.get("connect-src"), "'self' http://127.0.0.1:54321 ws://127.0.0.1:54321");
});

test("Header je Stufe", () => {
  assert.deepEqual(cspHeaders("aus", INPUT), { forNext: null, response: [] });

  const streng = cspHeaders("streng", INPUT);
  assert.equal(streng.forNext, strictPolicy(INPUT));
  assert.deepEqual(streng.response, [["Content-Security-Policy", strictPolicy(INPUT)]]);

  // Beim Beobachten NUR Report-Only: Ein scharfer Header ohne script-src aus dem Proxy verdeckt Next die Nonce
  // (Next kopiert die Antwort-Header des Proxys auf die Anfrage und liest zuerst Content-Security-Policy).
  const beobachten = cspHeaders("beobachten", INPUT);
  assert.equal(beobachten.forNext, strictPolicy(INPUT));
  assert.deepEqual(beobachten.response, [["Content-Security-Policy-Report-Only", strictPolicy(INPUT)]]);
  // nur report-uri: mit report-to daneben lieferte Chrome keine Meldungen aus
  assert.ok(!strictPolicy(INPUT).includes("report-to"));
});

test("Grundschutz: fester Header aus next.config.ts, nur im Produktions-Build, gleich dem in der strengen Policy", async () => {
  assert.equal(BASELINE_POLICY, "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  assert.ok(strictPolicy(INPUT).includes(BASELINE_POLICY));
  assert.deepEqual(securityHeaders(true).at(-1), { key: "Content-Security-Policy", value: BASELINE_POLICY });
  assert.ok(!securityHeaders(false).some((h) => h.key === "Content-Security-Policy"));
  for (const production of [true, false]) {
    const names = securityHeaders(production).map((h) => h.key);
    for (const name of ["X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Permissions-Policy"]) {
      assert.ok(names.includes(name), `${name} fehlt`);
    }
  }
  // Mikrofon nur fuer die eigene Seite (Gespraech), Kamera und Bildschirmaufnahme nie
  const permissions = securityHeaders(true).find((h) => h.key === "Permissions-Policy")?.value ?? "";
  for (const part of ["microphone=(self)", "camera=()", "display-capture=()"]) assert.ok(permissions.includes(part), part);
});

test("Meldungen: altes Format (report-uri)", () => {
  const text = JSON.stringify({
    "csp-report": {
      "document-uri": "https://nieeeki.example/?weg=direkt&geheim=1#x",
      "violated-directive": "script-src-elem",
      "effective-directive": "script-src-elem",
      "blocked-uri": "https://boese.example/x.js?token=abc",
      "source-file": "https://nieeeki.example/_next/static/chunks/a.js?v=1",
      "line-number": 12,
      disposition: "report",
    },
  });
  assert.deepEqual(parseCspReports(text), [
    {
      directive: "script-src-elem",
      blocked: "https://boese.example/x.js",
      document: "https://nieeeki.example/",
      source: "https://nieeeki.example/_next/static/chunks/a.js",
      line: 12,
      disposition: "report",
    },
  ]);
});

test("Meldungen: Reporting API (report-to), Schluesselwoerter bleiben, Fremdes faellt weg", () => {
  const text = JSON.stringify([
    { type: "deprecation", body: { id: "x" } },
    { type: "csp-violation", body: { effectiveDirective: "script-src", blockedURL: "eval", documentURL: "https://n.example/a", disposition: "enforce" } },
    { type: "csp-violation", body: { effectiveDirective: "img-src", blockedURL: "data:image/png;base64,AAAA", lineNumber: -3 } },
    { type: "csp-violation", body: "kein objekt" },
  ]);
  assert.deepEqual(parseCspReports(text), [
    { directive: "script-src", blocked: "eval", document: "https://n.example/a", source: "", line: null, disposition: "enforce" },
    { directive: "img-src", blocked: "data:", document: "", source: "", line: null, disposition: "" },
  ]);
});

test("Meldungen: nichts Ungeprueftes ins Log (Steuerzeichen, Laenge, Menge, Muell)", () => {
  const boese = JSON.stringify({
    "csp-report": { "effective-directive": "x\n2026 ERROR gefaelscht\u001b[31m", "blocked-uri": "ä".repeat(500) },
  });
  const [report] = parseCspReports(boese);
  assert.equal(report.directive, "x?2026 ERROR gefaelscht?[31m");
  assert.equal(report.blocked.length, 200);
  assert.match(report.blocked, /^\?+$/);

  const viele = JSON.stringify(
    Array.from({ length: 20 }, () => ({ type: "csp-violation", body: { effectiveDirective: "img-src" } })),
  );
  assert.equal(parseCspReports(viele).length, 2);
  // andere Schemata nur als Schema (blob: enthielte sonst die Herkunft doppelt)
  const schemata = JSON.stringify({ "csp-report": { "effective-directive": "worker-src", "blocked-uri": "blob:https://n.example/1234" } });
  assert.equal(parseCspReports(schemata)[0].blocked, "blob:");

  for (const text of ["", "kein json", "null", "42", "{}", "[]", '{"csp-report": []}', '{"csp-report": {"blocked-uri": "x"}}']) {
    assert.deepEqual(parseCspReports(text), []);
  }
  const zuLang = JSON.stringify({ "csp-report": { "effective-directive": "img-src", x: "y".repeat(MAX_REPORT_CHARS) } });
  assert.deepEqual(parseCspReports(zuLang), []);
});
