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
export interface HudFeedSession {
  id: string;
  /** What to call it on screen; `null` when the feed named it nothing. */
  label: string | null;
  status: string | null;
  /** `null` = the prompt index couldn't be read. NOT `0`. */
  pending: number | null;
  /** The parked prompt's id — the half of a permission event's identity. */
  elicitationId: string | null;
  /** Epoch **milliseconds** (the feed reports seconds; converted here). */
  updatedAtMs: number | null;
  costUsd: number | null;
  /** The DECLARED limit, the only denominator a percentage may use. */
  maxCostUsd: number | null;
  /** The session has a budget whose limit could not be read — not a number. */
  budgetUnreadable: boolean;
}

export interface HudFeedReport {
  readable: boolean;
  exact: boolean;
  stale: boolean;
  /** The row list is a page, not the whole set — per-session facts are partial. */
  truncated: boolean;
  /**
   * The SERVER's clock when it built this snapshot, in epoch milliseconds, or
   * `null` when it didn't say. Row ages are measured between this and
   * `updatedAtMs` — two readings of the same clock — so a desktop whose own
   * clock is off cannot invent (or hide) a stalled session.
   */
  generatedAtMs: number | null;
  active: number;
  awaiting: number;
  /** Sessions the feed could not resolve or had to leave out. */
  unresolved: number;
  /**
   * WHICH sessions are blocked on a human, not just how many. The shell needs
   * the identity to tell attention it has already shown the user from attention
   * that just arrived — a count cannot: a permission that stays pending would
   * otherwise re-open the panel on every poll. Only trustworthy alongside
   * `readable && exact && !stale`; the shell checks that for itself.
   */
  awaitingIds: string[];
  /**
   * Whether the sessions below account for EVERY session in scope — the active
   * view plus every one that settled inside the grace window. `false` means a
   * session missing from the list may simply not have been carried, so its
   * absence proves nothing (it never licenses ignoring a row that IS present;
   * those are proven either way).
   */
  observationComplete: boolean;
  /**
   * The rows themselves, so the shell can notice per-session moments a tally
   * cannot express: a prompt that just appeared, a run that just ended, spend
   * crossing a declared budget, a session that stopped moving.
   *
   * Carries the active view AND the sessions that just settled — the latter
   * are how a completion is witnessed at all, since the active view drops a
   * session the moment it finishes. Only meaningful alongside
   * `readable && exact && !stale && !truncated && unresolved === 0`; the shell
   * checks that for itself.
   */
  sessions: HudFeedSession[];
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
