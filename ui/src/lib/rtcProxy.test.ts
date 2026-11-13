// Tests fuer rtcProxy.ts: das Offer ablegen und auf die Antwort des Voice-Cores warten (Weg B, Schritt 5).
// Ausfuehren: npm test   (im Ordner ui/; nutzt den eingebauten Test-Runner von Node, kein Netz, keine Datenbank)
import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_BODY_CHARS, bearerToken, parseIceServers, parseOffer, relayOffer, type Deps } from "./rtcProxy.ts";

const ENV = { url: "https://x.supabase.co/", key: "sb_publishable_TEST", schema: "nieeeki_voice_test" };
const TOKEN = "NUTZER-TOKEN-GEHEIM";
const ID = "0b9e2d7a-3c5f-4f6e-9a47-1d2c3b4a5f60";
const OFFER = { sdp: "v=0\r\no=- 1", type: "offer", pc_id: null, restart_pc: false };
const ANSWER = { sdp: "v=0 antwort", type: "answer", pc_id: "pc-1" };

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

type Call = { url: string; init?: RequestInit };

/** Fake-Datenbank: antwortet der Reihe nach; sleep stellt die Uhr vor. */
function fixture(responses: Array<Response | Error>) {
  const calls: Call[] = [];
  let t = 0;
  const deps: Deps = {
    fetch: async (url, init) => {
      calls.push({ url, init });
      const next = responses.shift() ?? json([], 200);
      if (next instanceof Error) throw next;
      return next;
    },
    sleep: async (ms) => {
      t += ms;
    },
    now: () => t,
  };
  return { calls, deps, clock: () => t };
}

test("bearerToken", () => {
  assert.equal(bearerToken("Bearer abc.def"), "abc.def");
  assert.equal(bearerToken("bearer abc"), "abc");
  for (const h of [null, undefined, "", "Bearer", "Basic abc", "Bearer a b", "abc"]) assert.equal(bearerToken(h), null, String(h));
});

test("parseOffer: gueltige Offers", () => {
  assert.deepEqual(parseOffer(JSON.stringify({ sdp: "v=0", type: "offer", pc_id: null, restart_pc: false })), {
    sdp: "v=0", type: "offer", pc_id: null, restart_pc: false,
  });
  assert.deepEqual(parseOffer(JSON.stringify({ sdp: "v=0", type: "offer" })), { sdp: "v=0", type: "offer", pc_id: null, restart_pc: false });
  const neu = parseOffer(JSON.stringify({ sdp: "v=0", type: "offer", pc_id: "pc-1", restart_pc: true }));
  assert.equal(neu?.pc_id, "pc-1");
  assert.equal(neu?.restart_pc, true);
});

test("parseOffer: requestData des Clients wird verworfen", () => {
  const offer = parseOffer(JSON.stringify({ sdp: "v=0", type: "offer", requestData: { body: { anlass: "weckwort" } } }));
  assert.ok(offer);
  assert.equal("requestData" in offer, false);
  assert.equal("request_data" in offer, false);
});

test("parseOffer: Signatur und Zeit des Geraets werden durchgereicht, aber nur in der erwarteten Form", () => {
  const gut = { sdp: "v=0", type: "offer" };
  const signiert = parseOffer(JSON.stringify({ ...gut, sig: "abc_-", ts: 1800000000 }));
  assert.equal(signiert?.sig, "abc_-");
  assert.equal(signiert?.ts, 1800000000);
  assert.equal("sig" in (parseOffer(JSON.stringify(gut)) ?? {}), false); // ohne Signatur bleibt der Body wie bisher
  assert.equal(parseOffer(JSON.stringify({ ...gut, ts: 1e11 - 1 }))?.ts, 1e11 - 1); // letzte erlaubte Zeit
  assert.equal("sig" in (parseOffer(JSON.stringify({ ...gut, sig: null })) ?? {}), false); // null = nicht gesetzt
  const schlecht = [
    { sig: "" }, { sig: "x".repeat(201) }, { sig: 5 }, { sig: {} }, { sig: "a b" }, { sig: "ab+/=" }, { sig: "abc\n" },
    { ts: "1800000000" }, { ts: 1.5 }, { ts: 0 }, { ts: -1 }, { ts: 1e11 }, { ts: 1e12 }, { ts: true },
  ];
  for (const extra of schlecht) assert.equal(parseOffer(JSON.stringify({ ...gut, ...extra })), null, JSON.stringify(extra));
});

test("parseOffer: Abweichungen werden abgewiesen", () => {
  const schlecht = [
    "", "kein json", "null", "[]", "42", '"offer"', "{}",
    JSON.stringify({ sdp: "v=0", type: "offer", extra: 1 }),
    JSON.stringify({ sdp: "v=0", type: "answer" }),
    JSON.stringify({ type: "offer" }),
    JSON.stringify({ sdp: "", type: "offer" }),
    JSON.stringify({ sdp: 42, type: "offer" }),
    JSON.stringify({ sdp: "v=0", type: "offer", pc_id: "" }),
    JSON.stringify({ sdp: "v=0", type: "offer", pc_id: "x".repeat(101) }),
    JSON.stringify({ sdp: "v=0", type: "offer", pc_id: 5 }),
    JSON.stringify({ sdp: "v=0", type: "offer", restart_pc: "ja" }),
    JSON.stringify({ sdp: "x".repeat(MAX_BODY_CHARS), type: "offer" }),
  ];
  for (const text of schlecht) assert.equal(parseOffer(text), null, text.slice(0, 50));
});

test("relayOffer: legt das Offer als Nutzer ab und gibt die Antwort zurueck", async () => {
  const { calls, deps } = fixture([
    json([{ id: ID }], 201),
    json([{ status: "pending", response: null }]),
    json([{ status: "claimed", response: null }]),
    json([{ status: "answered", response: { ...ANSWER, geheimes_extra: "nie weitergeben" } }]),
  ]);
  const result = await relayOffer(OFFER, TOKEN, ENV, deps);
  assert.deepEqual(result, { status: 200, body: ANSWER });

  const [insert, ...polls] = calls;
  assert.equal(insert.url, "https://x.supabase.co/rest/v1/voice_signals");
  const headers = insert.init?.headers as Record<string, string>;
  assert.equal(insert.init?.method, "POST");
  assert.equal(headers.apikey, ENV.key);
  assert.equal(headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(headers["Content-Profile"], "nieeeki_voice_test");
  assert.deepEqual(JSON.parse(String(insert.init?.body)), { request: OFFER });
  assert.equal(polls.length, 3);
  assert.equal(polls[0].url, `https://x.supabase.co/rest/v1/voice_signals?id=eq.${ID}&select=status,response`);
  assert.equal((polls[0].init?.headers as Record<string, string>)["Accept-Profile"], "nieeeki_voice_test");
});

test("relayOffer: Fehler beim Ablegen werden zu Statuscodes, 4xx beendet den Client, 5xx laesst ihn neu versuchen", async () => {
  const faelle: Array<[number, number, string]> = [
    [401, 401, "anmeldung"], [403, 403, "verboten"], [429, 429, "zu_viele_anfragen"],
    [400, 400, "form"], [413, 400, "form"], [404, 502, "datenbank"], [500, 503, "datenbank"], [503, 503, "datenbank"],
  ];
  for (const [db, status, error] of faelle) {
    const { deps, calls } = fixture([json({ code: "X" }, db)]);
    const r = await relayOffer(OFFER, TOKEN, ENV, deps);
    assert.deepEqual(r, { status, body: { error } }, `DB ${db}`);
    assert.equal(calls.length, 1, "nach einem Fehler wird nicht weiter nachgefragt");
  }
  const netz = fixture([new Error("kein Netz")]);
  assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, netz.deps), { status: 503, body: { error: "datenbank" } });
});

test("relayOffer: unerwartete oder boesartige Zeilen-ID wird nie in eine URL gesetzt", async () => {
  for (const rows of [[], [{}], [{ id: 5 }], [{ id: "x&select=*" }], [{ id: "../../etc" }], {}]) {
    const { deps, calls } = fixture([json(rows, 201)]);
    assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, deps), { status: 502, body: { error: "unerwartet" } });
    assert.equal(calls.length, 1);
  }
});

test("relayOffer: Kurzcodes des Voice-Cores werden zu Statuscodes", async () => {
  const faelle: Array<[string | undefined, number, string]> = [
    ["owner", 403, "owner"], ["geraet", 403, "geraet"], ["form", 400, "form"], ["wiederholt", 400, "wiederholt"],
    ["handler", 502, "handler"], ["abbruch", 503, "abbruch"], ["etwas-neues", 502, "voice_core"], [undefined, 502, "voice_core"],
  ];
  for (const [code, status, error] of faelle) {
    const response = code === undefined ? {} : { error: code };
    const { deps } = fixture([json([{ id: ID }], 201), json([{ status: "failed", response }])]);
    assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, deps), { status, body: { error } }, String(code));
  }
});

test("relayOffer: ohne Antwort 504 nach dem Zeitlimit (Client versucht es dann neu)", async () => {
  // Die Zeile bleibt 'pending': der Voice-Core antwortet nie
  const pending = Array.from({ length: 100 }, () => json([{ status: "pending", response: null }]));
  const f = fixture([json([{ id: ID }], 201), ...pending]);
  const r = await relayOffer(OFFER, TOKEN, ENV, f.deps, { timeoutMs: 5_000, pollMs: 500 });
  assert.deepEqual(r, { status: 504, body: { error: "keine_antwort" } });
  assert.equal(f.clock(), 5_000); // genau bis zum Zeitlimit gewartet, nicht laenger
  assert.equal(f.calls.length, 1 + 10); // ein Ablegen und 10 Nachfragen im 500-ms-Takt
});

test("relayOffer: verschwundene Zeile (aufgeraeumt) ergibt sofort 504", async () => {
  const { deps, calls } = fixture([json([{ id: ID }], 201), json([])]);
  assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, deps), { status: 504, body: { error: "keine_antwort" } });
  assert.equal(calls.length, 2);
});

test("relayOffer: kurze Ausfaelle beim Nachfragen werden uebersprungen", async () => {
  const { deps } = fixture([
    json([{ id: ID }], 201),
    new Error("Netz weg"),
    json({ message: "kurz weg" }, 503),
    json([{ status: "answered", response: ANSWER }]),
  ]);
  assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, deps), { status: 200, body: ANSWER });
});

test("relayOffer: abgelaufene Anmeldung beim Nachfragen ist ein 4xx", async () => {
  for (const s of [401, 403]) {
    const { deps } = fixture([json([{ id: ID }], 201), json({}, s)]);
    assert.deepEqual(await relayOffer(OFFER, TOKEN, ENV, deps), { status: s, body: { error: "anmeldung" } });
  }
});

test("relayOffer: 'answered' ohne sdp zaehlt nicht als Antwort", async () => {
  const f = fixture([json([{ id: ID }], 201), json([{ status: "answered", response: { type: "answer" } }]), ...Array.from({ length: 20 }, () => json([{ status: "pending" }]))]);
  const r = await relayOffer(OFFER, TOKEN, ENV, f.deps, { timeoutMs: 2_000, pollMs: 500 });
  assert.equal(r.status, 504);
});

test("parseIceServers: nur oeffentliche STUN-Adressen, nie Zugangsdaten", () => {
  assert.deepEqual(parseIceServers('[{"urls": ["stun:stun.example.org:3478"]}]'), [{ urls: ["stun:stun.example.org:3478"] }]);
  assert.deepEqual(parseIceServers('[{"urls": "stuns:stun.example.org"}]'), [{ urls: ["stuns:stun.example.org"] }]);
  // TURN und Zugangsdaten gehoeren nicht in eine NEXT_PUBLIC-Variable (waere fuer jeden lesbar)
  assert.deepEqual(parseIceServers('[{"urls": ["turn:t.example.org"], "username": "u", "credential": "c"}]'), []);
  assert.deepEqual(parseIceServers('[{"urls": ["stun:a.example.org"], "username": "u", "credential": "c"}]'), [{ urls: ["stun:a.example.org"] }]);
  for (const raw of [undefined, "", "  ", "kein json", "{}", "[1]", '[{"urls": []}]', '[{"username": "u"}]', '[{"urls": ["http://x"]}]']) {
    assert.deepEqual(parseIceServers(raw), [], String(raw));
  }
});

test("relayOffer: das Token steht nie im Ergebnis", async () => {
  const ergebnisse = [
    await relayOffer(OFFER, TOKEN, ENV, fixture([json([{ id: ID }], 201), json([{ status: "answered", response: ANSWER }])]).deps),
    await relayOffer(OFFER, TOKEN, ENV, fixture([json({}, 500)]).deps),
    await relayOffer(OFFER, TOKEN, ENV, fixture([json([{ id: ID }], 201), json([{ status: "failed", response: { error: "handler" } }])]).deps),
  ];
  for (const r of ergebnisse) assert.equal(JSON.stringify(r).includes(TOKEN), false);
});
