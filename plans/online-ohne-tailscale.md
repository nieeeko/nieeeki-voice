# Bauplan: nieeeki online ohne Tailscale-App (Phase 2)

Stand 04.11.2026, Version 2 (nach kritischem Review gegen den installierten Code: Pipecat 1.12,
small-webrtc-transport 1.11, client-js 1.14, aiortc). Ziel: Handy und fremde Rechner erreichen nieeeki über
`https://nieeeki.nieeeko.com`, ohne Tailscale-App, ohne dass der Laptop (Vodafone Station, DS-Lite, kein
eingehendes IPv4) oder der Voice-Core vom Internet aus erreichbar wird. Nur Nikolaj darf eine Sitzung starten.

## Fakten aus dem Code (geprüft)

- Der Voice-Core nutzt den Pipecat-Runner mit **einem** Transport: `transport_params = {"webrtc": ...}`
  (`voice-core/bot.py:255`); `AudioHold` klinkt sich in `write_audio_frame` ein (`playback.py:71-83`).
- `auth_gate.py` schützt jede HTTP-Anfrage des Runners: Signatur, Owner (`sub == NIEEEKI_OWNER_ID`), aal2 und
  Anmeldealter über `amr` (Web 1 Tag, Desktop 1 Woche). Es setzt einen erreichbaren Runner voraus.
- **Der Browser-Client kann nicht ohne HTTP signalisieren.** Offer, ICE-Kandidaten (PATCH), Neuverhandlung und
  ICE-Neustart gehen immer per `fetch` an einen Endpunkt (client-js `index.module.js:1416-1443`,
  small-webrtc-transport `index.module.js:2958, 3006, 3117`). Es gibt keinen Haken für eigene Signalisierung.
  Möglich ist aber `client.connect({ webrtcRequestParams: { endpoint, headers }, iceConfig })`, also ohne
  `/start` und mit freiem Endpunkt (`index.d.ts:12-19`).
- Serverseitig geht es im Prozess: `SmallWebRTCRequestHandler(ice_servers=...)` mit `handle_web_request(...)`
  (`pipecat/transports/smallwebrtc/request_handler.py:97-237`); `bot()` bekommt die Verbindung wie im Runner
  (`runner/run.py:1013-1022`). aiortc sammelt alle ICE-Kandidaten schon beim Erzeugen der Antwort.
- Der Client legt immer drei Transceiver an. Steht ICE schon, wenn die Pipeline startet, verlangt der Server eine
  **Neuverhandlung**; bei ICE `failed`/`disconnected` startet der Client mit `restart_pc` neu. Ein einzelnes
  Offer pro Sitzung reicht also nicht.
- Der Client erzeugt `RTCPeerConnection` mit den `iceServers`, die er **vor** dem ersten Offer hat (Standard:
  leer, also weder STUN noch TURN).
- aiortc nutzt nur den ersten STUN- und den ersten TURN-Eintrag, `turns:` nur über TCP
  (`aiortc/rtcicetransport.py:108-136`).
- Vercel hält bewusst nur `NEXT_PUBLIC_*`-Werte. Das soll so bleiben.

## Entscheidung (Schritt 0, braucht Nikolaj)

Das Kernproblem ist die **Verabredung**: Wie erfährt der Voice-Core hinter DS-Lite, dass eine Sitzung gewünscht
ist? Beide Wege lösen das mit Supabase-Tabellen, die der Voice-Core nur ausgehend beobachtet.

| | **B: SmallWebRTC + TURN-Relay** (empfohlen) | **A: Daily-Transport** (bisher im README) |
| --- | --- | --- |
| Medienweg | DTLS-SRTP und Datenkanal Ende-zu-Ende zwischen Browser und Voice-Core. Der TURN-Betreiber sieht Metadaten: IPs, Zeiten, Datenmengen, Paketmuster | SFU des Anbieters. Bei SFUs ist üblich, dass sie je Teilnehmer entschlüsseln; für Daily nicht geprüft |
| Voice-Core-Umbau | Transport bleibt: `AudioHold`, Video-Ausgang, RTVI, `session_guard` unverändert | Transport getauscht: `pipecat-ai[daily]`, `AudioHold` und Video-Ausgang neu prüfen |
| Oberfläche | Signalisierungs-Proxy `/api/rtc` (Schritt 5) | `@pipecat-ai/daily-transport` statt `small-webrtc-transport` |
| Neues Konto | TURN-Anbieter mit kurzlebigen Zugangsdaten | Daily-Konto mit API-Schlüssel (nur im Voice-Core) |
| Risiko | Neuverhandlung, ICE-Daten vor dem Offer, Vercel-Wartezeit | Latenz und Innehalten ungeprüft; Bildschirm über Dritte |

Empfehlung: **B.** Der Bildschirm-Stream zeigt den ganzen PC; er soll nicht über die SFU eines Dritten laufen.
Offen vor dem Start: TURN-Anbieter samt Kontingent und Preis (nicht geprüft), und eine Wahrheit zur Echtheit:
Die DTLS-Fingerprints laufen über Supabase und Vercel; wer Zeilen verändern kann (`service_role`), könnte sich
dazwischenschalten. Deshalb schreibt außer dem Voice-Core niemand mit `service_role`.

### Sicherheitsentscheidung, die Tailscale heute abdeckt (Fund W2)

Ohne Tailscale reicht **ein gestohlenes aal2- oder Refresh-Token** (liegt im localStorage,
`ui/src/lib/supabase.ts:39-71`) für volle Fernsteuerung: Getippter Text zählt wie Nikolaj (`bot.py:446`), und
Klicken und Tippen im Live-Bild laufen ohne Stimmprüfung (`bot.py:470-481`). Darum gilt:
**Die Tailscale-Pflicht entfällt erst, wenn eine Gerätebindung steht** (Schritt 6): Passkey/WebAuthn bei jedem
Sitzungsstart oder ein nicht exportierbarer Geräteschlüssel, der Zeilen-ID, Offer-Hash und Zeit signiert, und der
Voice-Core prüft die Signatur. Bis dahin bleibt der neue Weg ein Zusatz, nicht der Ersatz.

## Invarianten (nach jedem Schritt prüfen)

- Voice-Core-Tests grün (bekannt zeitabhängig rot: zwei Tests in `test_innehalten_pipeline`, auch auf `main`).
- Kein Geheimnis in `ui/` oder Vercel (`service_role`, TURN-Schlüssel, Tokens): REA/Grep auf das
  Produktions-Bundle nach jedem Deploy (README, Abschnitt Desktop-App).
- Der Weg „direkt“ (Tailscale/localhost) funktioniert weiter. Desktop-App und Weckwort bleiben auf HTTP über
  localhost; abgeschaltet wird am Ende nur `tailscale serve`.
- Neue Tabellen: RLS wie `0001`, FK-Indizes, nichts für `anon`, `.down.sql`, Agent `database-reviewer` vor
  jedem PROD-Apply (CLAUDE.md).
- Zeileninhalte (SDP, Body, Zugangsdaten) werden nie geloggt (Pipecat loggt SDP und Body auf Debug-Ebene).

## Schritte (Reihenfolge nach Review)

### 1. Spike: Signalisierungs-Proxy und Neuverhandlung (zuerst)
- **Fragen:** (a) Trägt `connect({ webrtcRequestParams: { endpoint: "/api/rtc", headers }, iceConfig })` den
  ganzen Ablauf inklusive Neuverhandlung (`pc_id`) und Neustart (`restart_pc`)? (b) Wie oft tritt die
  Neuverhandlung wirklich auf (messen, nicht ableiten)? (c) Wie lange darf eine Vercel-Funktion warten (Plan und
  Limit prüfen, nicht geprüft)? (d) Kann der Voice-Core `ask_to_renegotiate` mit Test abschalten
  (`enableCam:false`)? Das wäre ein Monkeypatch.
- **Ergebnis (erledigt 04.11.2026, aus dem Paketcode belegt, small-webrtc-transport 1.11.0, Pipecat 1.12):**
  Der Client schickt nur **POST-Offers** `{sdp, type, pc_id, restart_pc, requestData}` (`index.module.js:2989-3006`),
  die Antwort ist `{sdp, type, pc_id}`. Die ICE-Kandidaten stecken im Offer, wenn der Transport mit
  `waitForICEGathering: true` gebaut wird (Standard `false`; er wartet höchstens 2 s, `:2979`). Die PATCH-Anfragen
  mit `{pc_id, candidates}` (`:2950-2962`) schickt er nach der Antwort, ein Fehler dabei wird nur geloggt; der Proxy
  beantwortet sie also ohne Datenbank. Ein **4xx** auf das Offer beendet den Client sofort (`isOfferRefused`,
  `:3022`), ein 5xx oder Timeout lässt ihn nach 2 s neu versuchen. Neuverhandlung und ICE-Neustart sind weitere
  POST-Offers mit `pc_id` bzw. `restart_pc: true` (`:2852-2860`, `:2989-2994`). Serverseitig akzeptiert
  `SmallWebRTCRequest.from_dict` beide Schreibweisen (`requestData`/`request_data`), der Runner reicht sie als
  `runner_args.body` an den Bot (`run.py:1013-1022`). Damit genügt eine Tabelle für Offers (Fragen a, c, d aus
  dem Review geklärt). Offen bleibt (b): Wie oft die Neuverhandlung wirklich auftritt, misst Schritt 7.
  **Modell:** Standard.

### 2. Migration `0002_signalisierung` (Abhängigkeit: 1) — Stand 04.11.2026: DEV und TEST angewendet
- **Datei:** `supabase/migrations/0002_signalisierung.sql` und `.down.sql`. Tabelle `voice_signals` (`user_id` per
  Default `auth.uid()`, `request` und `response` als jsonb, `status` `pending|claimed|answered|failed`,
  `created_at`, `expires_at` höchstens 60 s). `voice_ice` kommt mit Schritt 3.
- **Rechte:** `authenticated` darf nur lesen und `insert (request)`; kein Update, kein Delete, keine Spalten
  `user_id`/`status`/`expires_at`. Die Rechte aus `0001` gelten nur für damals vorhandene Tabellen. RLS ist gleich
  nach `create table` eingeschaltet, vor allen Rechten.
- **RLS:** Profil-Gate wie `0001` (zwei Policies) plus eine RESTRICTIVE-Policy für alle Rollen: `aal2` und
  Anmeldealter höchstens 1 Tag (frühester `amr`-Zeitstempel, nur echte Zahlen, wie `auth_gate.py`).
  Der Voice-Core prüft zusätzlich `user_id = NIEEEKI_OWNER_ID`.
- **Missbrauch:** Trigger je Nutzer serialisiert (Advisory-Lock): höchstens 6 Anfragen je Minute und 60 gespeicherte
  Zeilen, sonst `PT429` (PostgREST macht daraus HTTP 429). `claim_voice_signal(p_user_id)` übernimmt atomar
  (`for update skip locked`, Ablauf nach Datenbankuhr). `purge_voice_signals(interval)` räumt auf, nie jünger als 2
  Minuten (Rate-Limit-Fenster), Standard 10 Minuten. Beide nur für `service_role`.
- **Aufbewahrung:** Nach dem Verbindungsaufbau ersetzt der Voice-Core `request` durch einen Platzhalter
  `{"sdp":"","type":"offer"}` und setzt `response` auf null, **löscht aber nicht sofort** (der Zähler sieht nur
  vorhandene Zeilen); die Zeile geht per `purge_voice_signals`. Bewusst nicht in der Realtime-Publikation.
- **Geprüft** (TEST, Datei exakt; DEV nachweislich strukturell identisch in Constraints, Policies, Funktionen,
  Indizes, Tabellen-, Spalten- und Funktionsrechten, RLS): aal1, fremdes oder fehlendes Profil, anon, fremde
  `user_id`, `status`/`expires_at` setzen, Update/Delete/Truncate-Rechte, Anmeldung 2 Tage alt, `amr` fehlt, Zeitstempel als
  Text und `"NaN"`, sieben Formfehler (ohne `sdp`/`type`, falscher `type`, `sdp` kein Text oder null, kein
  Objekt, über 32 KB), Zeilenflut als Schleife und als Array-Insert, Gesamtgrenze 60, `claim` nur für
  `service_role` und genau einmal je Zeile, abgelaufene Zeilen nicht übernommen, `purge` nur für `service_role` mit
  Mindestalter. Der Review (ECC `database-reviewer`) fand einen Blocker (Down-Reihenfolge) und drei wichtige Punkte
  (Zähler sieht gelöschte Zeilen nicht, Race ohne Sperre, kein Aufräumen); alle behoben. Eigener Fund beim Test:
  `CHECK` lässt `NULL` passieren, darum `coalesce` bei `type` und `sdp`.
- **Noch nicht geprüft:** Der Rundlauf Down → Up. Der Supabase-Connector lehnt `drop table`/`drop function` in
  dieser Sitzung ab; die Reihenfolge der Down-Datei ist begründet (Funktionen am Zeilentyp zuerst), aber nicht
  ausgeführt. Vor PROD einmal in TEST mit Bestätigung laufen lassen. Ebenso offen: 20 parallele Inserts.
- **Weiter:** PROD erst mit Nikolajs Go.
- **Ausstieg:** `.down.sql`. **Modell:** stark.

### 3. ICE-Zugangsdaten vor dem ersten Offer (parallel zu 4; Abhängigkeit: 2, Schritt 0)
- **Aufgaben:** Der Voice-Core holt je Sitzung kurzlebige TURN-Zugangsdaten und legt sie rotierend in `voice_ice`,
  sodass der Client sie **vor** dem Offer hat. Pro Sitzung per `update_ice_servers`; in der Liste zuerst die
  UDP-TURN-URL, dazu eine STUN-URL (aiortc nimmt nur je den ersten Eintrag). Lebensdauer länger als ein
  Gespräch oder Erneuerung; ein ICE-Neustart baut mit den alten Daten neu auf. Alternative zur Tabelle: Variante
  mit zwei Phasen (Anfrage, dann ICE-Daten, dann Offer), kostet eine Runde mehr.
- **Anbieter-Kandidat Cloudflare Realtime TURN** (recherchiert, siehe unten): `POST
  https://rtc.live.cloudflare.com/v1/turn/keys/{KEY_ID}/credentials/generate-ice-servers` mit `{"ttl": Sekunden}`
  und dem API-Token als Bearer liefert `iceServers` (STUN plus TURN mit UDP 3478/443, TCP 3478/80, TLS 5349/443;
  die UDP-URLs stehen zuerst). Vorzeitiger Widerruf per `.../credentials/{username}/revoke`. Der TURN-Schlüssel ist
  ein langlebiges Geheimnis und gehört nur in die `.env` des Voice-Cores. Grenzen für `ttl` nennt die Doku nicht
  (im Test ausprobieren). Port 53 meiden: Browser blockieren ihn, ohne Trickle-ICE läuft der Aufbau sonst in
  einen Timeout.
- **Prüfen:** Test mit gefälschtem Anbieter; Schlüssel nie im Log; Ablauf und Erneuerung.

### 4. Rendezvous im Voice-Core (Abhängigkeit: 2)
- **Aufgaben:** `voice-core/rendezvous.py` beobachtet ausgehend `voice_signals`. Es darf **nicht** `import bot`
  aufrufen (zweite `SingleSession`, zwei parallele Gespräche); `bot` kommt aus `__main__`, der Lauscher startet
  über den Lifespan von `app`. Jede Zeile ist ein Offer: ohne `pc_id` neues `bot()` als Task mit fester
  Referenz, mit `pc_id` (Neuverhandlung) oder `restart_pc` derselbe Handler, ohne neues `bot()`. Eigener
  `SmallWebRTCRequestHandler`, nicht `ConnectionMode.SINGLE`. `body` (Begrüßung) und Kanal (`phone`) kommen aus
  der Zeile. Übernahme eines laufenden Gesprächs erst bei `connected`; identische Offers per Hash abweisen.
- **Abfrage:** Im `.venv` fehlt ein Realtime-Client. **Zuerst Abfrage alle 2 s** (robust, im Mittel etwa 1 s je
  Runde, doppelt mit Neuverhandlung), Realtime erst später als Optimierung: Für supabase-py gibt es einen
  gemeldeten Fehler, bei dem ein asynchrones `subscribe` nie `SUBSCRIBED` erreicht (Issue #1319, supabase-py 2.24.0 /
  realtime 2.65.2; ob behoben, nicht geprüft). Voraussetzung für Realtime ist außerdem, dass die Tabelle in der
  Publikation `supabase_realtime` steht, sonst kommen stumm keine Ereignisse. Im UI nach dem Abonnieren einmal
  direkt abfragen.
- **Pflichten aus Schritt 2:** Übernahme über `claim_voice_signal(NIEEEKI_OWNER_ID)`; `purge_voice_signals()` beim
  Start und etwa jede Minute; nach dem Verbindungsaufbau `request` durch den Platzhalter ersetzen und `response`
  leeren (nicht löschen); `response` klein halten und bei Fehlern nie SDP loggen; eine **eigene Drossel im
  Voice-Core**: höchstens ein Handler-Start gleichzeitig, auch wenn die Datenbank bis zu 6 Zeilen je Minute zulässt.
- **Tests (zuerst):** abgelaufen, fremder Owner, doppelte Zeile, Handler-Fehler, Neuverhandlung, Neustart,
  Übernahme erst bei `connected`. **Ausstieg:** `NIEEEKI_RENDEZVOUS=0`. **Modell:** stark.

### 5. Oberfläche: Proxy `/api/rtc` und Umschalter (Abhängigkeit: 1, 3, 4)
- **Aufgaben:** Route Handler `ui/src/app/api/rtc/route.ts` auf derselben Domain. POST: Zeile mit dem Nutzer-Token
  und dem Publishable Key anlegen (RLS prüft), auf die Antwort warten, zurückgeben. PATCH: annehmen oder
  ignorieren. Das Warten auf die Antwort bleibt unter einer Minute; Vercel erlaubt laut Doku (Stand Februar 2026)
  300 s auf Hobby und bis zu 800 s auf Pro, das reicht deutlich. Vercel braucht keine Geheimnisse. `useNieeeki.ts`: Umschalter `NEXT_PUBLIC_TRANSPORT=direkt|
  rendezvous` (Standard `direkt`), Fehlertext in `useNieeeki.ts:383` nennt noch Tailscale und wird angepasst.
- **Vertrag des Proxys (aus Schritt 1 und 2):** Der Transport wird mit `waitForICEGathering: true` gebaut. POST
  prüft den Body (nur `sdp`, `type: "offer"`, `pc_id`, `restart_pc`, `requestData` als bekannte Schlüssel, unter
  32 KB) und legt `{request: <Body>}` als Nutzer an. Antwort der Datenbank `42501` wird 403, `PT429` wird 429, ein
  Formfehler 400; das sind 4xx und beenden den Client sofort. Wartet die Route länger als 25 s, antwortet sie 504,
  das ist ein 5xx und der Client versucht nach 2 s neu. PATCH beantwortet die Route mit 200, ohne Datenbank. Der
  Client braucht ein Backoff, denn 6 Anfragen je Minute sind bei Mobilfunk-Flattern knapp.
- **Prüfen:** UI-Build, Lint, Typen; REA-Prüfung des Bundles auf Geheimnisse.

### 6. Gerätebindung (Abhängigkeit: 5; vor dem Abschalten der Tailscale-Pflicht)
- **Aufgaben:** Passkey/WebAuthn je Sitzungsstart oder Geräteschlüssel, der Zeilen-ID, Offer-Hash und Zeit
  signiert; der Voice-Core prüft die Signatur gegen den bei der Einrichtung hinterlegten Schlüssel (nur Nikolaj
  richtet ein, nur am Laptop). Ohne gültige Signatur startet nichts, auch mit gültigem Token.
- **Prüfen:** Test „gestohlenes Token ohne Gerät" scheitert. `security-reviewer`, `agent-security-hardening`.

### 7. Live-Test und Latenz (Nikolaj am Handy)
- **Aufgaben:** Handy im Mobilfunk ohne Tailscale: Sitzung, drei Gespräche, Bildschirm zeigen, Unterbrechung,
  Weckwort-Gespräch. Messen (`latency-critical-systems`): Zeit bis Antwortzeile, TURN-Allocation,
  Verbindungsaufbau, erster Ton; Vergleich mit Tailscale (≈ 1 bis 1,5 s bis zum ersten Ton). IPv6 kann eine
  direkte Verbindung ohne Relay erlauben (DS-Lite hat natives IPv6): im Log festhalten, ob Relay oder direkt.

### 8. Abschluss
- **Aufgaben:** README: neue Anleitung, `tailscale serve` abschalten (README:173), Runner und `auth_gate` auf
  localhost bleiben. Regel festlegen: Das Weckwort zu Hause darf eine Handy-Sitzung übernehmen (heute schon so).
  PROD-Migration mit Go.

## Parallelität
1 zuerst. Dann 2. Danach 3 und 4 gleichzeitig (andere Dateien). 5 nach 1, 3, 4. 6 nach 5. 7 und 8 am Ende.

## Abweichungen für Weg A (Daily)
Schritte 3 und 4 entfallen: Der Voice-Core legt per Daily-REST Raum und Tokens an (Schlüssel nur lokal) und
schreibt `room_url` und `user_token` in eine Zeile. `pipecat-ai[daily]`, `transport_params["daily"]` mit
Video-Ausgang, `AudioHold` und Video-Ausgang neu prüfen, Oberfläche wechselt auf `@pipecat-ai/daily-transport`.
Gerätebindung (Schritt 6) bleibt Pflicht. Vorab klären, dass der Bildschirm über den Anbieter laufen darf.

## Recherche vom 04.11.2026 (Web, nicht am eigenen System getestet)
- **Cloudflare Realtime TURN:** Abrechnung nach Ausgang, 0,05 US-Dollar je GB; die ersten 1000 GB je Monat sind
  frei (gemeinsames Kontingent mit dem SFU-Produkt); STUN ist frei und unbegrenzt. Eigene Überschlagsrechnung,
  nicht gemessen: Ein Bildschirm-Stream mit 2 MBit/s sind etwa 0,9 GB je Stunde, 1000 GB reichen also für gut 1000
  Stunden, Sprache allein ist vernachlässigbar. Voraussetzung ist ein Cloudflare-Konto (der DNS bleibt bei Hostinger).
  Quellen: [Preise](https://developers.cloudflare.com/realtime/pricing/),
  [Zugangsdaten erzeugen](https://developers.cloudflare.com/realtime/turn/generate-credentials/).
- **Vercel-Funktionen:** Mit Fluid Compute gelten laut Doku (Stand Februar 2026) 300 s Standard und Höchstwert auf
  Hobby, 300 s Standard und 800 s Höchstwert auf Pro. Der Signalisierungs-Proxy braucht unter einer Minute.
  Quelle: [Vercel, Funktionsdauer](https://vercel.com/docs/functions/limitations).
- **Supabase Realtime in Python:** gemeldeter Fehler beim asynchronen Abonnieren
  ([supabase-py #1319](https://github.com/supabase/supabase-py/issues/1319)), Behebungsstand unbekannt; deshalb
  beginnt der Plan mit Abfrage alle 2 s. Tabellen müssen in `supabase_realtime` stehen
  ([Doku Postgres Changes](https://supabase.com/docs/guides/realtime/postgres-changes)).

## Nicht geprüft
Realtime `postgres_changes` zusammen mit `auth.jwt()->>'aal'` in RLS zur Laufzeit; pg_cron; tatsächliche Default
Privileges in der Datenbank; Cloudflare: Grenzen für `ttl`, Verhalten bestehender Verbindungen nach Ablauf oder
Widerruf; Daily (SFU oder P2P, Verschlüsselung); IPv6 und UDP im Mobilfunk; heutiger Wert von
`NEXT_PUBLIC_VOICE_CORE_URL` auf Vercel.

## Offene Fragen an Nikolaj
1. Weg B (empfohlen) oder A?
2. Gerätebindung: Passkey (Handy und Laptop) oder eigener Geräteschlüssel? Ohne sie bleibt Tailscale Pflicht.
3. TURN-Anbieter: Cloudflare Realtime TURN (Konto anlegen, TURN-Schlüssel erzeugen) ist der Kandidat; ein
   Datenschutz-OK für das Relay (Metadaten, keine Inhalte) wäre vorab nötig.
