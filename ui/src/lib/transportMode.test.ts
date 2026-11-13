// Tests fuer transportMode.ts: Welcher Weg gilt, direkt (Tailscale) oder rendezvous (ueber Supabase)?
// Ausfuehren: npm test   (im Ordner ui/)
import assert from "node:assert/strict";
import { test } from "node:test";

import { resolveTransport } from "./transportMode.ts";

const kein = { search: "", stored: null };

test("ohne alles gilt der Standard der Umgebung, sonst direkt", () => {
  assert.deepEqual(resolveTransport({ envDefault: "rendezvous", ...kein }), { mode: "rendezvous" });
  assert.deepEqual(resolveTransport({ envDefault: "direkt", ...kein }), { mode: "direkt" });
  for (const env of [undefined, "", "irgendwas", "Rendezvous "]) {
    assert.deepEqual(resolveTransport({ envDefault: env, ...kein }), { mode: "direkt" }, String(env));
  }
});

test("?weg= in der Adresse gewinnt und wird gemerkt", () => {
  assert.deepEqual(resolveTransport({ envDefault: "rendezvous", search: "?weg=direkt", stored: null }), { mode: "direkt", store: "direkt" });
  assert.deepEqual(resolveTransport({ envDefault: "direkt", search: "?weg=rendezvous", stored: null }), { mode: "rendezvous", store: "rendezvous" });
  assert.deepEqual(resolveTransport({ envDefault: "direkt", search: "?x=1&weg=rendezvous&y=2", stored: "direkt" }), { mode: "rendezvous", store: "rendezvous" });
});

test("das Gemerkte gilt, solange die Adresse nichts sagt", () => {
  assert.deepEqual(resolveTransport({ envDefault: "rendezvous", search: "", stored: "direkt" }), { mode: "direkt" });
  assert.deepEqual(resolveTransport({ envDefault: "direkt", search: "?foo=bar", stored: "rendezvous" }), { mode: "rendezvous" });
});

test("?weg=standard loescht das Gemerkte und nimmt wieder den Standard", () => {
  assert.deepEqual(resolveTransport({ envDefault: "rendezvous", search: "?weg=standard", stored: "direkt" }), { mode: "rendezvous", store: "clear" });
  assert.deepEqual(resolveTransport({ envDefault: "direkt", search: "?weg=standard", stored: "rendezvous" }), { mode: "direkt", store: "clear" });
});

test("ungueltige Werte werden ignoriert, nie gemerkt", () => {
  for (const search of ["?weg=", "?weg=irgendwas", "?weg=RENDEZVOUS", "?weg=direkt%20", "?wegx=direkt", "weg=direkt"]) {
    const r = resolveTransport({ envDefault: "rendezvous", search, stored: null });
    assert.deepEqual(r, { mode: "rendezvous" }, search);
  }
  // ein kaputtes Gemerktes (z. B. von Hand veraendert) wird ignoriert
  assert.deepEqual(resolveTransport({ envDefault: "direkt", search: "", stored: "boese" }), { mode: "direkt" });
  assert.deepEqual(resolveTransport({ envDefault: "rendezvous", search: "", stored: "" }), { mode: "rendezvous" });
});
