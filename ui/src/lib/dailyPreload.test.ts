// Test fuer dailyPreload.ts: Das vorab angelegte Daily-Objekt wirkt nur, wenn App und WebRTC-Transport dieselbe
// Kopie von @daily-co/daily-js laden (ein Modul, ein Register fuer getCallInstance). Bei einer zweiten Kopie (etwa
// wenn der Transport auf eine neue Daily-Version springt) fiele das Vorladen still weg und Daily liefe wieder ueber eval.
// Ausfuehren: npm test   (im Ordner ui/)
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

test("App und Transport laden dieselbe Kopie von @daily-co/daily-js", () => {
  const appRequire = createRequire(import.meta.url);
  const transportRequire = createRequire(appRequire.resolve("@pipecat-ai/small-webrtc-transport"));
  assert.equal(appRequire.resolve("@daily-co/daily-js"), transportRequire.resolve("@daily-co/daily-js"));
});
