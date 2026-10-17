# nieeeki-voice — Hinweise für Claude Code

Monorepo: `ui/` (Next.js 16), `voice-core/` (Python, Pipecat 1.x), `host/` (Python, MCP v1),
`supabase/` (SQL-Migrationen mit `__SCHEMA__`-Platzhalter).

## ECC-Skills (affaan-m/everything-claude-code), die hier gelten

| Arbeit | Skill / Agent |
| --- | --- |
| Mehrschrittige Features planen | `blueprint` |
| SQL, Migrationen, RLS | `database-migrations`, `postgres-patterns`, Agent `database-reviewer` vor jedem PROD-Apply |
| PC-Host, Werkzeuge, Freigaben | `agent-security-hardening`, `mcp-server-patterns` |
| Latenz im Voice-Pfad | `latency-critical-systems` (p50/p95 je Segment messen) |
| React/Next.js | `frontend-patterns`, `react-patterns` |
| Vor jedem PR | `verification-loop` (Build, Typen, Lint, Tests, Secret-Scan, Diff) |

Installation der Skills: `/plugin install ecc@ecc` oder einzelne Skills nach `.claude/skills/` kopieren.

## Regeln

- Migrationen sind nach PROD-Apply unveränderlich; Änderungen als neue Migration mit `.down.sql`.
- Jede neue Tabelle: RLS mit Profil-Gate wie in 0001, FK-Indizes, keine Rechte für `anon`.
- `service_role`-Key nur im Voice-Core, nie in `ui/`.
- Neue PC-Werkzeuge im Host: Risikoklasse festlegen, im Host prüfen (nicht im Prompt), Audit-Eintrag.
- Sprachausgaben von nieeeki: kurz, kein Markdown (siehe `voice-core/persona.py`).
