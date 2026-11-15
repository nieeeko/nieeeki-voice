import type { Metadata, Viewport } from "next";
import localFont from "next/font/local";
import { connection } from "next/server";
import "./globals.css";

// Kommandozentrale: breite Display-Schrift fuer Marke und grosse Zahlen, Mono fuer HUD-Beschriftungen.
// Die Schriften liegen im Repo (fonts/, SIL Open Font License, latin-Teil von Google Fonts, Stand 15.11.2026) statt
// ueber next/font/google: Google liefert JetBrains Mono inzwischen ueber eine Adresse mit Parametern
// (/l/font?kit=...&skey=...), daran scheitert der Schriftlader von Turbopack ("queries have exactly one entry").
// Das brach seit dem 14.11. die lokale Oberflaeche (jede Seite 500) und am 15.11. den Vercel-Build. So braucht weder
// der Build noch die Desktop-App Google, und die Content-Security-Policy kommt mit font-src 'self' aus.
const display = localFont({
  variable: "--font-display",
  src: "./fonts/unbounded-latin-var.woff2",
  weight: "500 800",
  display: "swap",
});

const hud = localFont({
  variable: "--font-hud",
  src: "./fonts/jetbrains-mono-latin-var.woff2",
  weight: "400 700",
  display: "swap",
});

const body = localFont({
  variable: "--font-body",
  src: [
    { path: "./fonts/atkinson-hyperlegible-latin-400.woff2", weight: "400", style: "normal" },
    { path: "./fonts/atkinson-hyperlegible-latin-700.woff2", weight: "700", style: "normal" },
  ],
  display: "swap",
});

export const metadata: Metadata = {
  title: "nieeeki",
  description: "Nikolajs Sprachassistentin",
  // Als App auf dem iPhone-Home-Bildschirm: eigenes Symbol, Statusleiste ueber dem Hintergrund
  appleWebApp: { title: "nieeeki", statusBarStyle: "black-translucent" },
  icons: { apple: "/icons/apple-touch-icon.png" },
};

// "cover" zeichnet bis unter Notch und Home-Leiste; die Abstaende setzt globals.css (safe-area).
// Die Oberflaeche ist bewusst immer dunkel (Kommandozentrale).
export const viewport: Viewport = {
  viewportFit: "cover",
  themeColor: "#07070a",
  colorScheme: "dark",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // Je Anfrage rendern: Nur so traegt jedes Skript die Nonce der Content-Security-Policy (src/proxy.ts).
  await connection();
  return (
    <html lang="de" className={`${display.variable} ${hud.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
