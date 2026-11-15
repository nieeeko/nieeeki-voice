import Daily from "@daily-co/daily-js";

/**
 * Der WebRTC-Transport (SmallWebRTCTransport) verwaltet Mikrofon und Geraete ueber Dailys Bibliothek. Die holt beim
 * ersten Gebrauch ein Skript von c.daily.co und fuehrt es standardmaessig per fetch + Function(...) aus. Das braucht
 * 'unsafe-eval' und passt nicht zur strengen Content-Security-Policy (lib/csp.ts).
 *
 * Darum legen wir das Daily-Objekt selbst an und laden das Skript mit avoidEval (Laden per Skript-Element), BEVOR der
 * Transport gebaut wird. Der Transport nimmt ein vorhandenes Objekt (Daily.getCallInstance()) und muss dann nichts
 * mehr laden. avoidEval wird bei JEDEM Aufruf mitgegeben: startCamera() des Transports ersetzt dailyConfig, und ein
 * spaeteres load() auf demselben Objekt (etwa nach einem Fehlschlag) liefe sonst ueber eval.
 *
 * Bewusst ohne eigenes Zeitlimit: Ein abgelaufenes Vorladen liesse den Transport neu laden, dann ohne avoidEval
 * (unter NIEEEKI_CSP=streng gesperrt). Haengt c.daily.co, beendet der Watchdog in useNieeeki.ts den Versuch.
 * Scheitert das Vorladen (kein Netz), versucht es der Transport selbst; das klappt nur ohne strenge Policy.
 * Die Version von @daily-co/daily-js muss zu der des Transports passen (ein Modul, ein Objekt): package.json und
 * dailyPreload.test.ts.
 */
export async function preloadDailyWithoutEval(): Promise<void> {
  try {
    const call = Daily.getCallInstance() ?? Daily.createCallObject({ dailyConfig: { avoidEval: true } });
    await call.load({ dailyConfig: { avoidEval: true } });
  } catch (error) {
    console.warn("nieeeki: Daily-Skript nicht vorab geladen, der Transport versucht es selbst.", error);
  }
}
