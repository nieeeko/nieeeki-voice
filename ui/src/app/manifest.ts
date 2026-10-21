import type { MetadataRoute } from "next";

// Installierbar ohne App Store: iPhone Safari "Zum Home-Bildschirm", Chrome/Edge "App installieren".
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "nieeeki",
    short_name: "nieeeki",
    description: "Nikolajs Sprachassistentin",
    lang: "de",
    start_url: "/",
    scope: "/",
    display: "standalone",
    background_color: "#07070a",
    theme_color: "#07070a",
    icons: [
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
