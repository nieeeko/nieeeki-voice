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
  // Feste Sicherheits-Header fuer jede Antwort (lib/csp.ts), bewusst ohne Content-Security-Policy: Ein CSP-Header hier
  // verdeckte auf Vercel die Nonce. Die strenge Policy kommt je Anfrage aus src/proxy.ts, der Grundschutz als <meta>
  // aus app/layout.tsx.
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders() }];
  },
};

export default nextConfig;
