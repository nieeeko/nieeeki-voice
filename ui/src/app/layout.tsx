import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Atkinson_Hyperlegible } from "next/font/google";
import "./globals.css";

const display = Bricolage_Grotesque({
  variable: "--font-display",
  subsets: ["latin"],
  weight: ["400", "600", "800"],
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

// "cover" zeichnet bis unter Notch und Home-Leiste; die Abstaende setzt globals.css (safe-area)
export const viewport: Viewport = {
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#e9efee" },
    { media: "(prefers-color-scheme: dark)", color: "#121b22" },
  ],
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="de" className={`${display.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
