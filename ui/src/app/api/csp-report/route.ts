import { MAX_REPORT_BYTES, parseCspReports, readLimitedText } from "@/lib/csp";

// Meldungen des Browsers ueber Verstoesse gegen die Content-Security-Policy (lib/csp.ts, report-uri).
// Bewusst ohne Anmeldung: Der Browser schickt sie ohne Token. Darum wird nichts gespeichert, nur eine kurze,
// bereinigte Zeile geloggt (feste Felder, druckbares ASCII, Adressen ohne Query, hoechstens 2 je Anfrage), und der
// Body wird hoechstens bis MAX_REPORT_BYTES gelesen. Lesen: Vercel-Laufzeit-Logs, Suche nach "csp-report".

const NO_CONTENT = () => new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });

export async function POST(req: Request) {
  let text: string | null;
  try {
    text = await readLimitedText(req.body, MAX_REPORT_BYTES);
  } catch {
    return NO_CONTENT(); // Client hat abgebrochen: kein Fehler fuers Log
  }
  if (text === null) return NO_CONTENT();
  for (const report of parseCspReports(text)) {
    console.warn(`csp-report ${JSON.stringify(report)}`);
  }
  return NO_CONTENT();
}
