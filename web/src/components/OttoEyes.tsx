import { FuchoMascot } from "@/components/FuchoMascot";

// The hero mascot on the new-chat landing. There's no session yet, so it rests
// idle — the animated Fucho, breathing gently, rather than a static logo.
export function OttoEyes({ className }: { className?: string }) {
  return <FuchoMascot pose="idle" size={72} className={className} ariaLabel="OmniCraft" />;
}
