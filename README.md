# nieeeki-voice

Nikolajs persönliche Sprachassistentin: Livegespräch mit natürlichen Unterbrechungen,
eine Oberfläche mit Stimmlinie und Canvas sowie Werkzeuge für den Windows-PC.

| Ordner | Was | Läuft auf |
| --- | --- | --- |
| `ui/` | Next.js 16: Stimmlinie (du oben, nieeeki unten, roter Strich = Unterbrechung), Canvas-Platzhalter | Vercel (später Desktop-App mit Tauri) |
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

**Noch nicht getestet**
- [ ] Erstes echtes Gespräch (Mikrofon → nieeeki → Lautsprecher) mit echten Schlüsseln.

## Hier weitermachen

1. **Erster Gesprächstest lokal** (siehe unten „Lokal starten“). Prüfen:
   - Zeit vom Ende deines Satzes bis zur Antwort
   - „warte mal, eigentlich …“ mitten in ihrer Antwort → stoppt sie sofort und knüpft an?
   - kurzes „mhm“ → darf sie *nicht* stoppen
2. **Gedächtnis einschalten:** nieeeki-Profil für Nikolajs Auth-User in `nieeeki_voice_dev` anlegen
   (nur per service_role möglich), dann `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
   `NIEEEKI_OWNER_ID` in `.env` setzen.
3. **Verlauf zusammenfassen**, damit Haiku unter 100.000 Tokens pro Prompt bleibt (darüber 5× teurer).
4. **Canvas-Tool** `canvas_show(type, title, data)` mit Renderern: Tabelle, Dokument, BPMN (bpmn-js), Mermaid/React Flow, Code.
5. **PC-Host anbinden:** Voice-Core ruft Host-Tools per MCP auf; kritische Aktionen über Tabelle `actions` freigeben.
6. **Weckwort „nieeeki“** (Porcupine oder openWakeWord), danach Desktop-App mit Tauri.
7. **Runpod-Deployment:** WebRTC braucht UDP, Runpod leitet nur HTTP/TCP weiter → TURN-Server oder Daily-Transport nötig.
8. **Eigene Stimme:** Soniox-Voice-Cloning (≤ 2 Min sauberer Aufnahme), A/B-Test gegen Bibliotheksstimme.
9. **Sprechererkennung:** Stimmabdruck (ECAPA) in `speakers`, nur Nikolaj darf kritische Aktionen freigeben.

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
| `.env` (Hauptordner oder `voice-core/`) | Voice-Core (Python) | `SONIOX_API_KEY`, `ANTHROPIC_API_KEY`, später `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `NIEEEKI_OWNER_ID`, `VOYAGE_API_KEY` |
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
