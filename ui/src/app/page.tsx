"use client";

import { CommandCenter } from "@/components/CommandCenter";
import { LoginGate } from "@/components/LoginGate";
import { useNieeeki } from "@/lib/useNieeeki";

export default function Home() {
  return <LoginGate>{(signOut) => <Live signOut={signOut} />}</LoginGate>;
}

/** Erst nach dem Login: Verbindung zum Voice-Core und die Kommandozentrale. */
function Live({ signOut }: { signOut: () => void }) {
  return <CommandCenter n={useNieeeki()} signOut={signOut} />;
}
