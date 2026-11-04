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
- **Ergebnis:** Befund mit Codeverweisen; Tabellenform und Proxy hängen davon ab. **Modell:** Standard.

### 2. Migration `0002_signalisierung` (Abhängigkeit: 1)
- **Tabellen:** `voice_signals` (jede Anfrage eine Zeile: `user_id`, `kind` `offer|patch`, `pc_id`, `restart_pc`,
  `body`, `offer`, `answer`, `status`, `created_at`, `expires_at`) und `voice_ice` (rotierende, kurzlebige
  ICE-Zugangsdaten: nur lesbar für den Owner).
- **Rechte:** `authenticated` bekommt spaltenweise nur `select` und `insert (kind, pc_id, restart_pc, body,
  offer)`, kein `update`/`delete`. Die Grants aus `0001` gelten nur für damals vorhandene Tabellen; Default
  Privileges gibt es nur für `service_role`. `status`, `created_at`, `expires_at` nur per Default,
  `check (expires_at <= created_at + interval '60 seconds')`.
- **RLS:** RESTRICTIVE-Policy mit `(select auth.jwt()->>'aal') = 'aal2'` und Anmeldealter aus `amr` (höchstens 1
  Tag). Der Voice-Core prüft zusätzlich `user_id = NIEEEKI_OWNER_ID`.
- **Missbrauch:** höchstens eine offene Anfrage je Nutzer (partieller Unique-Index) und ein Rate-Limit-Trigger,
  schon hier. Atomarer Claim per RPC oder bedingtem Update mit `now()` aus der Datenbank (gegen Doppelverarbeitung
  und Uhrenabweichung). Nach dem Verbindungsaufbau `offer`, `answer`, ICE-Daten leeren (IPs, Zugangsdaten).
  Aufräumen abgelaufener Zeilen: pg_cron (Verfügbarkeit nicht geprüft) oder der Voice-Core löscht.
- **Weiteres:** Realtime-Publikation wie in `0001:270-277`; Index auf dem Fremdschlüssel `user_id`.
- **Prüfen:** Angriffstests in DEV (anderer Nutzer, aal1, anon, fremde Zeile, zu langes `expires_at`,
  zweite offene Anfrage, `\dp` für tatsächliche Rechte); `database-reviewer`; dann TEST; PROD mit Go.
- **Ausstieg:** `.down.sql`. **Modell:** stark.

### 3. ICE-Zugangsdaten vor dem ersten Offer (parallel zu 4; Abhängigkeit: 2, Schritt 0)
- **Aufgaben:** Der Voice-Core holt je Sitzung kurzlebige TURN-Zugangsdaten und legt sie rotierend in `voice_ice`,
  sodass der Client sie **vor** dem Offer hat. Pro Sitzung per `update_ice_servers`; in der Liste zuerst die
  UDP-TURN-URL, dazu eine STUN-URL (aiortc nimmt nur je den ersten Eintrag). Lebensdauer länger als ein
  Gespräch oder Erneuerung; ein ICE-Neustart baut mit den alten Daten neu auf. Alternative zur Tabelle: Variante
  mit zwei Phasen (Anfrage, dann ICE-Daten, dann Offer), kostet eine Runde mehr.
- **Prüfen:** Test mit gefälschtem Anbieter; Schlüssel nie im Log; Ablauf und Erneuerung.

### 4. Rendezvous im Voice-Core (Abhängigkeit: 2)
- **Aufgaben:** `voice-core/rendezvous.py` beobachtet ausgehend `voice_signals`. Es darf **nicht** `import bot`
  aufrufen (zweite `SingleSession`, zwei parallele Gespräche); `bot` kommt aus `__main__`, der Lauscher startet
  über den Lifespan von `app`. Jede Zeile ist ein Offer: ohne `pc_id` neues `bot()` als Task mit fester
  Referenz, mit `pc_id` (Neuverhandlung) oder `restart_pc` derselbe Handler, ohne neues `bot()`. Eigener
  `SmallWebRTCRequestHandler`, nicht `ConnectionMode.SINGLE`. `body` (Begrüßung) und Kanal (`phone`) kommen aus
  der Zeile. Übernahme eines laufenden Gesprächs erst bei `connected`; identische Offers per Hash abweisen.
- **Abfrage:** Im `.venv` fehlt ein Realtime-Client. Entweder neue Abhängigkeit oder Abfrage alle 2 s (im Mittel
  etwa 1 s je Runde, doppelt mit Neuverhandlung). Im UI nach dem Abonnieren einmal direkt abfragen.
- **Tests (zuerst):** abgelaufen, fremder Owner, doppelte Zeile, Handler-Fehler, Neuverhandlung, Neustart,
  Übernahme erst bei `connected`. **Ausstieg:** `NIEEEKI_RENDEZVOUS=0`. **Modell:** stark.

### 5. Oberfläche: Proxy `/api/rtc` und Umschalter (Abhängigkeit: 1, 3, 4)
- **Aufgaben:** Route Handler `ui/src/app/api/rtc/route.ts` auf derselben Domain. POST: Zeile mit dem Nutzer-Token
  und dem Publishable Key anlegen (RLS prüft), auf die Antwort warten, zurückgeben. PATCH: annehmen oder
  ignorieren. Vercel braucht keine Geheimnisse. `useNieeeki.ts`: Umschalter `NEXT_PUBLIC_TRANSPORT=direkt|
  rendezvous` (Standard `direkt`), Fehlertext in `useNieeeki.ts:383` nennt noch Tailscale und wird angepasst.
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

## Nicht geprüft
Realtime `postgres_changes` zusammen mit `auth.jwt()->>'aal'` in RLS zur Laufzeit; pg_cron; tatsächliche Default
Privileges in der Datenbank; TURN-Anbieter (Lebensdauer, Widerruf, Preis); Daily (SFU oder P2P, Verschlüsselung);
Laufzeitgrenzen von Vercel-Funktionen; IPv6 und UDP im Mobilfunk; heutiger Wert von `NEXT_PUBLIC_VOICE_CORE_URL`
auf Vercel.

## Offene Fragen an Nikolaj
1. Weg B (empfohlen) oder A?
2. Gerätebindung: Passkey (Handy und Laptop) oder eigener Geräteschlüssel? Ohne sie bleibt Tailscale Pflicht.
3. TURN-Anbieter: Konto bei einem Anbieter mit kurzlebigen Zugangsdaten anlegen.
