// Bridge to the Electron shell's floating HUD window.
//
// The HUD route is the ordinary SPA, so it also renders in a plain browser tab
// (which is how it's developed and tested). Everything here therefore degrades
// to a no-op when the shell isn't there — the panel still expands, it just
// doesn't resize a native window.
//
// The renderer only ever states intent (`expanded`); the main process picks the
// bounds. See `electron/src/hud_preload.js`.

interface HudShellApi {
  setExpanded: (expanded: boolean) => void;
}

declare global {
  interface Window {
    omnicraftHud?: HudShellApi;
  }
}

/** True when running inside the shell's HUD window. */
export function hasHudBridge(): boolean {
  return typeof window !== "undefined" && typeof window.omnicraftHud?.setExpanded === "function";
}

/**
 * Ask the shell to switch the HUD window between its collapsed and expanded
 * footprints. Never throws: an old or broken shell must not break the panel.
 */
export function setHudExpanded(expanded: boolean): void {
  try {
    window.omnicraftHud?.setExpanded(expanded);
  } catch {
    /* shell bridge unavailable or torn down — the panel still works */
  }
}
