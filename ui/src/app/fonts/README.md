# Schriften

Lokal ausgelieferte Schriften der Oberfläche (eingebunden in `../layout.tsx` über `next/font/local`).
Jeweils nur der latin-Teil (deckt Deutsch samt Umlauten und ß ab), heruntergeladen am 15.11.2026 von Google Fonts.

| Datei | Schrift | Gewichte | Lizenz |
| --- | --- | --- | --- |
| `unbounded-latin-var.woff2` | Unbounded (variabel) | 500 bis 800 | [OFL-unbounded.txt](OFL-unbounded.txt) |
| `jetbrains-mono-latin-var.woff2` | JetBrains Mono (variabel) | 400 bis 700 | [OFL-jetbrainsmono.txt](OFL-jetbrainsmono.txt) |
| `atkinson-hyperlegible-latin-400.woff2` | Atkinson Hyperlegible | 400 | [OFL-atkinsonhyperlegible.txt](OFL-atkinsonhyperlegible.txt) |
| `atkinson-hyperlegible-latin-700.woff2` | Atkinson Hyperlegible | 700 | [OFL-atkinsonhyperlegible.txt](OFL-atkinsonhyperlegible.txt) |

Alle drei stehen unter der SIL Open Font License 1.1 (Weitergabe mit Lizenztext erlaubt).

**Warum nicht `next/font/google`:** Google liefert JetBrains Mono inzwischen über eine Adresse mit Parametern
(`/l/font?kit=…&skey=…`). Daran scheitert der Schriftlader von Turbopack (Next 16.4.2) mit „next/font/google
queries have exactly one entry“. Das legte ab dem 14.11.2026 die lokale Oberfläche lahm (jede Seite 500) und am
15.11. den Vercel-Build. Lokal braucht weder der Build noch die Desktop-App das Netz.
