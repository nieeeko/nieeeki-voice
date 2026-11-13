// Tests fuer devicePairing.ts: Stand der Kopplung, Etikett, Fehlertexte (Weg B, Schritt 6).
// Ausfuehren: npm test   (im Ordner ui/)
import assert from "node:assert/strict";
import { test } from "node:test";

import { deviceLabel, pairErrorText, pairState } from "./devicePairing.ts";

const NOW = Date.parse("2026-11-13T10:00:00Z");

test("pairState: freigegeben, wartend, abgelaufen, nichts", () => {
  assert.equal(pairState({ status: "active", expires_at: "2020-01-01T00:00:00Z" }, NOW), "gekoppelt"); // Ablauf gilt nur fuer wartende
  assert.equal(pairState({ status: "pending", expires_at: "2026-11-13T10:05:00Z" }, NOW), "wartet");
  assert.equal(pairState({ status: "pending", expires_at: "2026-11-13T09:59:59Z" }, NOW), "keine");
  assert.equal(pairState({ status: "pending", expires_at: "2026-11-13T10:00:00Z" }, NOW), "keine"); // genau jetzt = abgelaufen
  assert.equal(pairState(null, NOW), "keine");
  assert.equal(pairState(undefined, NOW), "keine");
});

test("pairState: unbrauchbare Zeilen zaehlen als nichts", () => {
  for (const row of [{}, { status: "pending" }, { status: "pending", expires_at: "gestern" }, { status: "pending", expires_at: 5 }, { status: "revoked" }]) {
    assert.equal(pairState(row, NOW), "keine", JSON.stringify(row));
  }
});

test("deviceLabel: erkennt gaengige Geraete, passt in 40 Zeichen", () => {
  assert.equal(deviceLabel("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"), "iPhone");
  assert.equal(deviceLabel("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)"), "iPad");
  assert.equal(deviceLabel("Mozilla/5.0 (Linux; Android 15; Pixel 9) Chrome/130"), "Android");
  assert.equal(deviceLabel("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130"), "Windows-PC");
  assert.equal(deviceLabel("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605"), "Mac");
  assert.equal(deviceLabel("Mozilla/5.0 (X11; Linux x86_64) Firefox/130"), "Linux");
  assert.equal(deviceLabel(""), "Browser");
  assert.ok(deviceLabel("irgendwas".repeat(50)).length <= 40);
});

test("pairErrorText: erklaert die haeufigen Faelle", () => {
  assert.match(pairErrorText({ code: "PT429" }, 429), /zehn Minuten/);
  assert.match(pairErrorText(null, 429), /zehn Minuten/);
  assert.match(pairErrorText({ code: "42501" }, 403), /Anmeldung/);
  assert.match(pairErrorText(null, 401), /Anmeldung/);
  assert.match(pairErrorText({ code: "42P01" }, 404), /Migration 0003/);
});
