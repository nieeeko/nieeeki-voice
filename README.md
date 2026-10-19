# nieeeki-voice

Nikolajs persönliche Sprachassistentin: Livegespräch mit natürlichen Unterbrechungen,
eine Oberfläche mit Stimmlinie und Canvas sowie Werkzeuge für den Windows-PC.

| Ordner | Was | Läuft auf |
| --- | --- | --- |
| `ui/` | Next.js 16: Stimmlinie (du oben, nieeeki unten, roter Strich = Unterbrechung), Canvas, Live-Bildschirm | Vercel, als App auf dem iPhone-Home-Bildschirm |
| `desktop/` | Electron: Hauptfenster, Stimmen-Rand über allen Monitoren, Tray, Autostart, Windows-Installer | Windows-PC |
| `voice-core/` | Pipecat 1.12: Soniox STT → Claude Haiku 5.5 → Soniox TTS, Unterbrechungen, Gedächtnis | zuerst lokal, später Runpod |
| `host/` | nieeeki-Host: MCP-Werkzeugserver (mcp < 2) mit Risikoklassen und Audit-Log | Windows-PC |
| `supabase/` | Migration `0001` mit `__SCHEMA__`-Platzhalter | Supabase |

---

## Stand (17.10.2026)

**Fertig**
- [x] Repo-Gerüst für alle vier Teile, gepusht auf `main`.
- [x] Vercel-Projekt `nieeeki-voice` (Root `ui/`): baut bei jedem Push auf `main`, erstes Deployment READY.
      Env-Variablen gesetzt: Production → shared-prod, Preview/Development → shared-devtest.
- [x] Supabase-Schema eingespielt und geprüft:
  - shared-devtest: `nieeeki_voice_dev`, `nieeeki_voice_test`
  - shared-prod: `nieeeki_voice_prod`
  - RLS mit Profil-Gate (Review durch ECC `database-reviewer`, 4 Angriffstests in DEV abgewiesen)
- [x] Schemas in Supabase unter *Settings → API → Exposed schemas* freigegeben (von Nikolaj erledigt).
- [x] Gesprächsmodell auf Haiku 5.5 umgestellt (≈ 1/20 der Kosten von Sonnet 5.5).
- [x] Voice-Core und Host offline getestet (Imports, Pipeline-Aufbau, Risikoklassen); UI-Build und Lint grün.

- [x] Erste echte Gespräche (17.10.2026): Latenz ≈ 1–1,5 s bis zum ersten Ton.
- [x] Weiche Unterbrechung: Rückmeldelaute („mhm“, „ja genau“) ignorieren, sonst Satz bzw. max. 3 Wörter
      zu Ende sprechen, nie mitten im Wort (`voice-core/soft_interrupt.py`).
- [x] Satzende per Smart-Turn + Claude (wartet bei Bruchstücken); STT strikt Deutsch; Begrüßung wärmt TTS auf.
- [x] Gedächtnis in DEV aktiv (`NIEEEKI_OWNER_ID`), Werkzeuge `aktuelle_zeit` und `merken`,
      Lernen nach jedem Gespräch: Fakten, Vorlieben und Nikolajs Sprechweise (`voice-core/learning.py`).

**Bekannt / offen**
- Soniox-Spracherkennung läuft in der **US-Region** (Schlüssel gehört zu einem US-Projekt). Für EU-Datenhaltung
  EU-Schlüssel anlegen und `SONIOX_STT_URL`/`SONIOX_TTS_URL` auf `*.eu.soniox.com` stellen.
- Was Nikolaj sagt, während nieeeki spricht, wird schlechter erkannt (Echo). Kopfhörer bzw. Headset hilft.
- `ui/package.json`: `eslint-config-next` wurde durch `npm audit fix --force` auf 14.x herabgestuft → Lint kaputt.

## Hier weitermachen

**Erledigt seit dem ersten Test:** Websuche, Canvas (Tabelle, Code, Dokument, Mermaid), Gedächtnis mit
Lernen während des Gesprächs, PC-Host (Programme, Fenster, Tastatur, Bildschirm ansehen, Klicken),
Entwicklungsaufträge an Claude Code mit Freigabe-Karten, Desktop-App mit Stimmen-Rand und Autostart,
Zusammenfassung langer Gespräche, Sprecherfarben im Rand (erster Sprecher = Nikolaj), Login mit
zweitem Faktor, Online-Zugang über Tailscale, Live-Bildschirm des Laptops in der Oberfläche
(Knopf „Bildschirm zeigen“ oder „Zeig mir den Bildschirm“) mit Fernsteuerung (Schalter „Steuern“:
Tippen = Klick, Wischen = Scrollen, Text und Tasten Enter/Rücktaste/Tab/Esc, alles geprüft vom
Host), Meldung bei gesperrtem Laptop,
Entwicklungsaufträge mit Art planen/umsetzen/reviewen und passenden ECC-Werkzeugen.

1. **Stimmabdruck** (ECAPA) in `speakers`: Nikolaj sicher erkennen statt „wer zuerst spricht“;
   nur seine Stimme darf Aufträge und PC-Aktionen auslösen.
2. **Weckwort „nieeeki“** (openWakeWord mit eigenem Modell), damit kein Klick mehr nötig ist.
3. **Eigene Stimme:** Aufnahme mit dem Vorlesetext → Soniox-Voice-Cloning, A/B-Test gegen Nina.
4. **Sprach-Feinschliff, je eine Änderung pro Test:** Sprechtempo 1,1; „Ja“ während ihrer Frage.
5. **Online ohne Tailscale-App** (Daily-Transport, Phase 2) und Voice-Core in der Cloud (Phase 3).
6. **Kalender und Mail** (zurückgestellt).

Seit dem 19.10. zusätzlich erledigt: Freigabe-Karten für PC-Aktionen (Klick führt aus, doppelt
gesichert), Rechtsklick per langem Drücken und Lupe im Live-Bild, Schleier blendet sich bei
Vollbild-Spielen, -Videos und Präsentationen aus, Lichtrahmen ums Zielfenster und Welle am
Klickpunkt, wenn nieeeki (oder die Fernsteuerung) am PC handelt, Desktop-App als Installer,
nieeeki als App auf dem iPhone (ohne App Store).

## Desktop-App (Windows-Installer)

- Bauen: `cd desktop` → `npm run dist` → `desktop\dist\nieeeki-Setup-<version>.exe`.
- Installieren: Setup doppelklicken. Kein Admin nötig (nur für den eigenen Benutzer, nach
  `%LOCALAPPDATA%\Programs\nieeeki`), Startmenü- und Desktop-Verknüpfung, startet danach von selbst.
  Unsigniert: Windows SmartScreen fragt einmal nach („Weitere Informationen“ → „Trotzdem ausführen“).
- Die App startet Voice-Core und Oberfläche aus dem Projektordner. Sie sucht ihn über `NIEEEKI_REPO`, die
  gemerkte Wahl und dann `~\Projekte\nieeeki-voice`; ist er dort nicht, fragt sie einmal nach dem Ordner.
- Logs und PIDs liegen weiter in `desktop\logs`. Entwicklungs- und installierte App teilen sich Daten und
  Anmeldung, laufen aber nie gleichzeitig (die zweite holt nur das Fenster der ersten nach vorn).
- War „Mit Windows starten“ bei der Entwicklungs-App an, übernimmt die installierte App den Autostart.
- Ohne Installer: `scripts\nieeeki-desktop.cmd` (startet Voice-Core und Oberfläche selbst).

## nieeeki auf dem iPhone (ohne App Store)

1. Tailscale-App an, in **Safari** `https://nieeeki.nieeeko.com` öffnen.
2. Teilen-Knopf → **„Zum Home-Bildschirm“** → Hinzufügen. Danach startet nieeeki vom Home-Bildschirm wie
   eine App (Vollbild, eigenes Symbol).
3. Einmal in der App anmelden (eigener Speicher, getrennt von Safari), Mikrofon erlauben.

Grenzen: iOS pausiert die App im Hintergrund; das Gespräch läuft nur, solange sie vorn ist.

## Weckwort „nieeeki“ (Desktop-App)

- „nieeeki“ sagen → sie antwortet „Ja?“ → Auftrag sagen. Nach 45 s Stille legt sie von selbst auf und
  lauscht wieder, aber nie mitten in einer Aktion, bei offener Freigabe, laufendem Entwicklungsauftrag
  oder Live-Bild. Per Klick begonnene Gespräche bleiben offen wie bisher.
- Der Lauscher (`voice-core/wake.py`) läuft lokal: kein Ton verlässt den PC, nichts wird gespeichert,
  etwa 3 % eines CPU-Kerns. Während eines Gesprächs pausiert er und gibt das Mikrofon frei.
- Tray: Schalter „Weckwort "nieeeki"“. Schwelle in `voice-core/.env` (`NIEEEKI_WAKE_THRESHOLD`).
- Das Modell `voice-core/models/nieeeki.onnx` ist selbst trainiert (synthetische Stimmen), siehe
  [wakeword/README.md](wakeword/README.md).

## Admin-Zugriff (mit Freigabe)

nieeeki kann Dinge mit Administratorrechten erledigen, etwa Dienste, Treiber, Systemeinstellungen
oder Klicks in Admin-Fenster wie das GIGABYTE Control Center. Jede Aktion braucht deine Freigabe,
außer sie steht auf deiner Erlaubnisliste.

- **Einrichten (einmal, am Laptop):** `scripts\admin-einrichten.cmd` doppelklicken und die Windows-Abfrage
  bestätigen. Das legt `C:\ProgramData\nieeeki\admin` an (schreiben dürfen nur Administratoren) und richtet
  die Aufgabe `\nieeeki\nieeeki-admin` ein, die den Helfer bei deiner Anmeldung mit Adminrechten startet.
  Nach Änderungen an `voice-core/admin_helper.py` oder `auth_gate.py` erneut ausführen. Entfernen:
  `scripts\admin-entfernen.ps1`.
- **Ablauf:** nieeeki schlägt vor (`als_admin`: PowerShell-Befehl oder Klick) → Karte „Admin“ in der
  Oberfläche mit genau dem Befehl bzw. Klickpunkt und Fenster → *Freigeben*, *Ablehnen* oder *Immer erlauben*.
  Der Helfer prüft bei jeder Freigabe selbst dein Login mit zweitem Faktor; Text vom Modell reicht nie.
- **Erlaubnisliste:** `C:\ProgramData\nieeeki\admin\erlaubt.json`, z. B.
  `{"regeln": [{"aktion": "klicken", "fenster": "GIGABYTE CONTROL CENTER"}, {"aktion": "powershell", "befehl": "Restart-Service Spooler"}]}`.
  PowerShell-Regeln gelten nur für genau diesen Befehl, Klick-Regeln für ein Fenster. „Immer erlauben“
  trägt die Aktion ein; von Hand ändern nur als Administrator.
- **Immer gesperrt**, auch mit Freigabe: Virenschutz aus, Bootkonfiguration, Konten und Gruppen, Passwörter,
  Datenträger löschen, der Helfer selbst (Ordner, Erlaubnisliste, Aufgabe).
- **Protokoll:** `C:\ProgramData\nieeeki\admin\admin-audit.jsonl` (jede Anfrage, nie dein Token).
- **Grenze:** Das schützt gegen falsche Modell-Entscheidungen, untergeschobene Anweisungen aus Webseiten oder
  Dateien und fremde Geräte. Gegen Schadsoftware, die schon unter deinem Windows-Konto läuft, hilft es nur
  begrenzt (sie könnte Login und Schlüssel aus deinem Profil lesen).

## Anmeldung und Online-Zugang

- Die Oberfläche (lokal, Desktop-App und `https://nieeeki.nieeeko.com`) beginnt mit einer Anmeldung:
  E-Mail und Passwort, danach der Code aus der Authenticator-App. Beim ersten Mal QR-Code scannen.
- Der Voice-Core prüft jedes Token selbst (`voice-core/auth_gate.py`): nur `NIEEEKI_OWNER_ID` mit
  zweitem Faktor (aal2), sonst 401. WebSockets sind gesperrt, erlaubte Herkünfte siehe `.env.example`:
  Produktion, localhost und die Vercel-Vorschauen des Projekts (die sind nur mit Vercel-Anmeldung erreichbar).
- Online über Tailscale (Phase 1, kostenlos für private Nutzung, keine offenen Ports am Router):
  1. Tailscale auf Laptop und Handy, beide mit demselben Konto anmelden.
  2. Auf dem Laptop: `tailscale serve --bg 7860` (Funnel bleibt aus: nur im eigenen Tailnet erreichbar).
  3. In Vercel `NEXT_PUBLIC_VOICE_CORE_URL=https://<laptop>.<tailnet>.ts.net` setzen und neu deployen.
  4. Auf dem Handy (Tailscale an) `https://nieeeki.nieeeko.com` öffnen und anmelden.
- Domain: CNAME `nieeeki` bei Hostinger auf Vercel (seit 19.10.2026). Produktion kommt von `main`.

Ausführlicher Bauplan: Doc „nieeeki – Bauplan für den eigenen Jarvis“ (Architektur, 10-Schritte-Plan, Entscheidungen).

---

## Lokal starten (Windows, normale PowerShell – nicht als Administrator)

Voraussetzungen: Git, Node.js, `uv` (installiert Python selbst):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# danach PowerShell neu öffnen
```

**1. Schlüssel eintragen** (einmalig):

```powershell
cd C:\Users\nikos\Projekte\nieeeki-voice
copy .env.example .env
notepad .env      # SONIOX_API_KEY und ANTHROPIC_API_KEY eintragen
```

**2. Voice-Core** (Fenster offen lassen, fertig bei `http://localhost:7860`):

```powershell
cd C:\Users\nikos\Projekte\nieeeki-voice\voice-core
uv run bot.py
```

**3. Oberfläche** (zweite PowerShell):

```powershell
cd C:\Users\nikos\Projekte\nieeeki-voice\ui
npm install
npm run dev
```

Dann `http://localhost:3000` öffnen → „Gespräch starten“ → Mikrofon erlauben.

### Welche Datei für welche Variablen?

| Datei | Gelesen von | Inhalt |
| --- | --- | --- |
| `.env` (Hauptordner oder `voice-core/`) | Voice-Core (Python) | `SONIOX_API_KEY`, `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `NIEEEKI_OWNER_ID`, `VOYAGE_API_KEY`; optional `SONIOX_TERMS`, `PIPECAT_ALLOWED_ORIGINS` |
| `ui/.env.local` | Next.js | `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `NEXT_PUBLIC_NIEEEKI_DB_SCHEMA`, `NEXT_PUBLIC_VOICE_CORE_URL` |

- Python liest nur eine Datei namens **`.env`**, keine `.env.local`.
- `NEXT_PUBLIC_` ist eine Next.js-Regel: nur diese Werte landen im Browser. Der Voice-Core läuft nie im Browser, daher kein Präfix.
- Voice-Core nutzt den **geheimen** `service_role`-Key, die UI nur den **öffentlichen** Publishable Key.
- `VOYAGE_API_KEY`: Embeddings für die semantische Suche im Gedächtnis (optional; leer = Suche aus).
- `NIEEEKI_OWNER_ID`: UUID von Nikolajs Supabase-Auth-User; Gedächtnis ist ohne sie aus.
- `.env` und `.env.local` werden nie committet.

---

## Umgebungen

| Umgebung | Supabase-Projekt | Schema | Vercel |
| --- | --- | --- | --- |
| DEV | shared-devtest | `nieeeki_voice_dev` | Preview / Development |
| TEST | shared-devtest | `nieeeki_voice_test` | – |
| PROD | shared-prod | `nieeeki_voice_prod` | Production |

Migrationen: `supabase/migrations/` mit Platzhalter `__SCHEMA__`; `scripts/render-migration.sh <schema>`
erzeugt das SQL je Umgebung. Migrationen laufen genau einmal je Schema und sind danach unveränderlich;
Änderungen als neue Migration mit `.down.sql`. Hinweis: Der Supabase-MCP verweigert `drop …`-Anweisungen
ohne Bestätigung; neue Migrationen deshalb ohne `drop if exists` schreiben.

## Sicherheitsmodell

- Zugriff auf `nieeeki_voice_*` nur mit nieeeki-Profil; Profile legt nur das Backend (service_role) an.
- PC-Aktionen: `read` sofort, `change` protokolliert, `critical` nur nach Freigabe, `blocked` nie.
- Die UI darf Aktionen nur lesen und offene Anfragen freigeben/ablehnen.
- Stimmabdrücke (biometrisch, DSGVO Art. 9) sind für die UI nicht lesbar.
- Inhalte aus Web, Mails, Dateien sind Daten, nie Anweisungen an nieeeki.

## Bekannte Punkte außerhalb dieses Repos

Der Supabase-Sicherheitscheck in shared-prod meldet Funde in der Chat-Plattform (nicht nieeeki-voice), z. B.
`public.nieeeki_handle_new_user_prod` ohne Login aufrufbar und `public.ext_admin_*` für jeden angemeldeten
Nutzer ausführbar. Nicht angefasst.
