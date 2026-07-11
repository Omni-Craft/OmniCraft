import { OttoIcon } from "@/components/icons/OttoIcon";

// The hero mascot. It previously animated Otto-the-starfish's eyes to follow the
// cursor; the mascot is now the OmniCraft fish logo (raster art), so this is a
// thin wrapper that renders it as a meaningful, labelled image.
export function OttoEyes({ className }: { className?: string }) {
  return (
    <OttoIcon
      className={["otto-float", className].filter(Boolean).join(" ")}
      role="img"
      aria-label="OmniCraft"
      aria-hidden={false}
    />
  );
}
