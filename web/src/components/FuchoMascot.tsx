import { useEffect, useRef } from "react";

import fuchoSheet from "@/assets/fucho-sheet.webp";
import estrelaSheet from "@/assets/estrela-sheet.webp";
import { cn } from "@/lib/utils";

// The animated OmniCraft mascot: the Fucho fish (and, optionally, its star
// buddy) rendered from a sprite sheet, crossfading between poses so it can
// REACT to what the app is doing — thinking, running a tool, erroring, done.
// Each pose is one row of the sheet (8 columns × 9 rows of 288px cells).

export type MascotPose =
  | "idle"
  | "frente"
  | "nadar"
  | "acenar"
  | "expressoes"
  | "erro"
  | "pensando"
  | "codando"
  | "feliz";

/** Row, frame count and per-frame pace for each pose (shared by both sheets). */
export const POSES: Record<MascotPose, { row: number; frames: number; ms: number }> = {
  idle: { row: 0, frames: 3, ms: 560 },
  frente: { row: 1, frames: 7, ms: 150 },
  nadar: { row: 2, frames: 8, ms: 130 },
  acenar: { row: 3, frames: 2, ms: 360 },
  expressoes: { row: 4, frames: 6, ms: 460 },
  erro: { row: 5, frames: 8, ms: 190 },
  pensando: { row: 6, frames: 6, ms: 240 },
  codando: { row: 7, frames: 7, ms: 165 },
  feliz: { row: 8, frames: 6, ms: 160 },
};

// The sheet is a grid of 288px cells: 8 columns × 9 rows (one row per pose).
const SHEET_COLS = 8;
const SHEET_ROWS = 9;

/** The background-size that scales one 288px cell down to `sizePx`. */
export function sheetBackgroundSize(sizePx: number): string {
  return `${SHEET_COLS * sizePx}px ${SHEET_ROWS * sizePx}px`;
}

/** The background-position for a given pose frame at `sizePx`. */
export function cellBackgroundPosition(pose: MascotPose, frame: number, sizePx: number): string {
  const { row } = POSES[pose];
  return `${-frame * sizePx}px ${-row * sizePx}px`;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * A two-layer crossfading sprite. Painting the incoming pose on the hidden
 * layer and swapping opacities dissolves one pose into the next instead of
 * hard-cutting. Returns a `show` to change pose and a `dispose`.
 */
function makeSprite(l0: HTMLElement, l1: HTMLElement, sheetUrl: string, sizePx: number) {
  const layers = [
    { el: l0, timer: null as ReturnType<typeof setInterval> | null },
    { el: l1, timer: null as ReturnType<typeof setInterval> | null },
  ];
  let active = 0;
  const still = prefersReducedMotion();
  for (const layer of layers) {
    layer.el.style.backgroundImage = `url(${sheetUrl})`;
    layer.el.style.backgroundSize = sheetBackgroundSize(sizePx);
  }
  function drive(layer: (typeof layers)[number], pose: MascotPose) {
    const spec = POSES[pose];
    let f = 0;
    if (layer.timer !== null) clearInterval(layer.timer);
    layer.timer = null;
    const paint = () => {
      layer.el.style.backgroundPosition = cellBackgroundPosition(pose, f, sizePx);
      f = (f + 1) % spec.frames;
    };
    paint();
    // Reduced motion: hold the first frame, never loop.
    if (!still) layer.timer = setInterval(paint, spec.ms);
  }
  return {
    show(pose: MascotPose) {
      const incoming = 1 - active;
      drive(layers[incoming], pose);
      layers[incoming].el.style.opacity = "1";
      layers[active].el.style.opacity = "0";
      const outgoing = active;
      active = incoming;
      window.setTimeout(() => {
        if (layers[outgoing].timer !== null) clearInterval(layers[outgoing].timer);
      }, 480);
    },
    dispose() {
      for (const layer of layers) {
        if (layer.timer !== null) clearInterval(layer.timer);
      }
    },
  };
}

export interface FuchoMascotProps {
  /** Which pose the fish holds. */
  pose: MascotPose;
  /** Pixel size of the fish cell (the star is scaled to ~0.7 of it). */
  size?: number;
  /** When set, the star buddy appears (lower-left) in this pose; null hides it. */
  starPose?: MascotPose | null;
  className?: string;
  /** Accessible name; decorative (empty) hides it from screen readers. */
  ariaLabel?: string;
}

/**
 * The reactive mascot. `pose` drives the fish; `starPose` optionally shows the
 * star companion for a beat (e.g. reacting to an error, celebrating a success).
 */
export function FuchoMascot({
  pose,
  size = 56,
  starPose = null,
  className,
  ariaLabel = "OmniCraft",
}: FuchoMascotProps) {
  const fishL0 = useRef<HTMLSpanElement>(null);
  const fishL1 = useRef<HTMLSpanElement>(null);
  const starL0 = useRef<HTMLSpanElement>(null);
  const starL1 = useRef<HTMLSpanElement>(null);
  const fish = useRef<ReturnType<typeof makeSprite> | null>(null);
  const star = useRef<ReturnType<typeof makeSprite> | null>(null);
  const starSize = Math.round(size * 0.7);

  // Build the sprite controllers once per size; rebuild if the size changes.
  useEffect(() => {
    if (!fishL0.current || !fishL1.current) return;
    fish.current = makeSprite(fishL0.current, fishL1.current, fuchoSheet, size);
    if (starL0.current && starL1.current) {
      star.current = makeSprite(starL0.current, starL1.current, estrelaSheet, starSize);
    }
    return () => {
      fish.current?.dispose();
      star.current?.dispose();
      fish.current = null;
      star.current = null;
    };
  }, [size, starSize]);

  useEffect(() => {
    fish.current?.show(pose);
  }, [pose]);

  useEffect(() => {
    if (starPose) star.current?.show(starPose);
  }, [starPose]);

  const layerStyle: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    backgroundRepeat: "no-repeat",
    imageRendering: "auto",
    opacity: 0,
    transition: "opacity 400ms ease-in-out",
  };

  return (
    <div
      className={cn("relative inline-block", className)}
      role={ariaLabel ? "img" : undefined}
      aria-label={ariaLabel || undefined}
      aria-hidden={ariaLabel ? undefined : true}
      style={{ width: size, height: size }}
    >
      {/* Star buddy, lower-left, behind the fish. Fades in only when posed. */}
      <span
        className="fucho-breathe"
        style={{
          position: "absolute",
          left: -Math.round(starSize * 0.5),
          bottom: -Math.round(size * 0.04),
          width: starSize,
          height: starSize,
          zIndex: 1,
          opacity: starPose ? 1 : 0,
          transition: "opacity 360ms ease-in-out",
        }}
      >
        <span ref={starL0} style={layerStyle} />
        <span ref={starL1} style={layerStyle} />
      </span>
      {/* Fish, in front. */}
      <span className="fucho-breathe" style={{ position: "absolute", inset: 0, zIndex: 2 }}>
        <span ref={fishL0} style={layerStyle} />
        <span ref={fishL1} style={layerStyle} />
      </span>
    </div>
  );
}
