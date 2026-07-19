// Bridge to the Electron shell's floating HUD window.
//
// The HUD route is the ordinary SPA, so it also renders in a plain browser tab
// (which is how it's developed and tested). Everything here therefore degrades
// to a no-op when the shell isn't there — the panel still expands, it just
// doesn't resize a native window.
//
// The renderer only ever states intent (`expanded`); the main process picks the
// bounds. See `electron/src/hud_preload.js`.

/**
 * What the panel tells the shell about the feed, so the shell can apply the
 * user's visibility mode. Every field carries its own uncertainty: `readable`
 * false (nothing could be read) or `exact` false (the counts are a floor) mean
 * the numbers are NOT an answer — the shell must not read them as idle.
 */
export interface HudFeedReport {
  readable: boolean;
  exact: boolean;
  stale: boolean;
  active: number;
  awaiting: number;
  /** Sessions the feed could not resolve or had to leave out. */
  unresolved: number;
}

interface HudShellApi {
  setExpanded: (expanded: boolean) => void;
  reportFeed?: (report: HudFeedReport) => void;
  onExpandedChanged?: (callback: (expanded: boolean) => void) => () => void;
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

/**
 * Tell the shell what the feed currently says. The shell owns the visibility
 * modes ("hide when idle", "only on attention") and this is its only source —
 * the main process has no authenticated session to poll the feed itself.
 */
export function reportHudFeed(report: HudFeedReport): void {
  try {
    window.omnicraftHud?.reportFeed?.(report);
  } catch {
    /* shell bridge unavailable or torn down — the panel still works */
  }
}

/**
 * Subscribe to the shell's own expand/collapse decisions (it auto-expands when
 * a session starts waiting on a human), so the panel shows the state its
 * window is actually in. Returns an unsubscribe; a no-op outside the shell.
 */
export function onHudExpandedChanged(callback: (expanded: boolean) => void): () => void {
  try {
    return window.omnicraftHud?.onExpandedChanged?.(callback) ?? (() => {});
  } catch {
    return () => {};
  }
}
