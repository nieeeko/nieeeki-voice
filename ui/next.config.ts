import type { NextConfig } from "next";
import { securityHeaders } from "./src/lib/csp";

const nextConfig: NextConfig = {
  // Bewusst ohne cacheComponents/partialPrefetching: Die Content-Security-Policy mit Nonce (src/proxy.ts) verlangt,
  // dass die Seite je Anfrage gerendert wird (eine vorab erzeugte Huelle haette Skripte ohne Nonce). Die Seite ist
  // ohnehin eine reine Client-Anwendung hinter dem Login, vorab erzeugt wurde nur ihre leere Huelle.
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // Feste Sicherheits-Header fuer jede Antwort, im Produktions-Build samt Grundschutz (lib/csp.ts). Die strenge
  // Policy kommt je Anfrage aus src/proxy.ts, weil sie eine frische Nonce braucht.
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders(process.env.NODE_ENV === "production") }];
  },
};

export default nextConfig;
