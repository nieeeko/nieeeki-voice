import type { Metadata, Viewport } from "next";
import { Atkinson_Hyperlegible, JetBrains_Mono, Unbounded } from "next/font/google";
import "./globals.css";

// Kommandozentrale: breite Display-Schrift fuer Marke und grosse Zahlen, Mono fuer HUD-Beschriftungen
const display = Unbounded({
  variable: "--font-display",
  subsets: ["latin"],
  weight: ["500", "700", "800"],
});

const hud = JetBrains_Mono({
  variable: "--font-hud",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const body = Atkinson_Hyperlegible({
  variable: "--font-body",
  subsets: ["latin"],
  weight: ["400", "700"],
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

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="de" className={`${display.variable} ${hud.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
