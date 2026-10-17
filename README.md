# nieeeki-voice

Nikolajs persönliche Sprachassistentin: Livegespräch mit natürlichen Unterbrechungen,
eine Oberfläche mit Stimmlinie und Canvas sowie Werkzeuge für den Windows-PC.

| Ordner | Was | Läuft auf |
| --- | --- | --- |
| `ui/` | Next.js-Oberfläche (Stimmlinie, Canvas, später Desktop-App mit Tauri) | Vercel |
| `voice-core/` | Pipecat-Pipeline: Soniox STT → Claude → Soniox TTS, Unterbrechungen, Gedächtnis | Runpod (Docker, CPU reicht) |
| `host/` | nieeeki-Host: MCP-Werkzeugserver auf dem PC, Freigabe-Klassen, Audit-Log | Windows-PC (Dienst) |
| `supabase/` | Schema `nieeeki_voice_<env>`: Sessions, Turns, Lernspeicher (pgvector), Canvas, Aktionen | Supabase |

## Lokal starten

```bash
# 1. Voice-Core
cd voice-core && cp .env.example .env   # Soniox- und Anthropic-Key eintragen
uv run bot.py                            # http://localhost:7860

# 2. Oberfläche
cd ui && npm install && npm run dev      # http://localhost:3000

# 3. PC-Host (auf dem Windows-PC)
cd host && uv run nieeeki_host.py
```

## Umgebungen

| Umgebung | Supabase-Projekt | Schema |
| --- | --- | --- |
| DEV | shared-devtest | `nieeeki_voice_dev` |
| TEST | shared-devtest | `nieeeki_voice_test` |
| PROD | shared-prod | `nieeeki_voice_prod` |

Migrationen liegen in `supabase/migrations/` mit Platzhalter `__SCHEMA__`;
`scripts/render-migration.sh <schema>` erzeugt das SQL je Umgebung.
Die Schemas müssen in Supabase unter *Settings → API → Exposed schemas* freigegeben sein.

## Sicherheitsmodell

- Zugriff hat nur, wer ein nieeeki-Profil hat; Profile legt nur das Backend an.
- PC-Aktionen: `read` sofort, `change` protokolliert, `critical` nur nach Freigabe, `blocked` nie.
- Die UI darf Aktionen nur lesen und offene Anfragen freigeben oder ablehnen.
- Stimmabdrücke (biometrisch) sind für die UI nicht lesbar.

Bauplan: siehe Doc „nieeeki – Bauplan für den eigenen Jarvis“.
