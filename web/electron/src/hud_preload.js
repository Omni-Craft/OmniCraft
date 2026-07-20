// Preload for the floating HUD window. Unlike the find bar's page, the HUD's
// content is the SERVER's own SPA — a remote page — so this bridge is
// deliberately the narrowest one in the shell: a single message carrying a
// single boolean.
//
// The renderer states its INTENT ("I want to be expanded"); it never names a
// size or a position. The main process owns the geometry and verifies the
// sending webContents is the live HUD, so a page that ends up with this
// preload attached can't grow an always-on-top window over the whole screen.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

/**
 * A finite number, or `null`. An absent measurement is never `0` — and a
 * numeric STRING is not a number either: `Number("3")` would launder a payload
 * we never agreed to into a count the shell prunes and alerts on.
 */
function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A real boolean, or `null`. "Not true" and "unknown" are different answers. */
function boolean(value) {
  return typeof value === "boolean" ? value : null;
}

/** A non-empty string, or `null`. */
function text(value) {
  return typeof value === "string" && value.length > 0 ? value : null;
}

/**
 * Per-session detail, validated at the boundary.
 *
 * The shell decides from this whether to claim a session just finished, is
 * blocked, or has stopped moving, so a row it cannot read must not travel as a
 * row it can. A field that doesn't match its shape becomes `null` — never a
 * coerced `0` or `""` — and a row without a usable id voids the WHOLE list:
 * a silently shortened list would let the shell believe a session it never saw
 * had gone away.
 *
 * @param {unknown} sessions
 * @returns {object[] | null}
 */
function sanitizeSessions(sessions) {
  if (!Array.isArray(sessions)) return null;
  const out = [];
  for (const session of sessions) {
    if (session === null || typeof session !== "object" || Array.isArray(session)) return null;
    const id = text(session.id);
    if (id === null) return null;
    out.push({
      id,
      label: text(session.label),
      status: text(session.status),
      pending: number(session.pending),
      elicitationId: text(session.elicitationId),
      updatedAtMs: number(session.updatedAtMs),
      costUsd: number(session.costUsd),
      maxCostUsd: number(session.maxCostUsd),
      // Three-valued like every other flag: this one says whether the row's
      // budget could be READ, and an unread readability marker is the last
      // thing that may collapse into "it was fine".
      budgetUnreadable: boolean(session.budgetUnreadable),
    });
  }
  return out;
}

contextBridge.exposeInMainWorld("omnicraftHud", {
  /**
   * Ask the shell to switch the HUD between the collapsed pill and the
   * expanded session list. The main process picks the bounds for each.
   * @param {boolean} expanded
   */
  setExpanded: (expanded) => {
    ipcRenderer.send("omnicraft:hud-set-expanded", expanded === true);
  },

  /**
   * Report what the feed says, so the shell can apply the user's visibility
   * mode (and expand when something starts waiting on a human). The HUD's page
   * is the only renderer that polls the feed — the main process has no
   * authenticated session of its own.
   *
   * Plain numbers/booleans so the payload survives structured cloning. Every
   * field is THREE-valued on the far side: true, false, or `null` for "this
   * edge could not read it". `readable` false or `exact` false means the counts
   * are not an answer, and the shell must not read them as "idle" — and a
   * `null` must not be read as either.
   *
   * `=== true` alone would be the wrong sanitizer here, and quietly so: it
   * turns an unreadable `truncated` into "the list is complete" and an
   * unreadable `unresolved` into `0` via `Number(null)`, which is the exact
   * shape of proof the shell demands before it prunes state and fires alerts.
   *
   * @param {{readable: boolean, exact: boolean, stale: boolean,
   *   truncated: boolean, observationComplete: boolean,
   *   generatedAtMs: number, active: number,
   *   awaiting: number, unresolved: number, awaitingIds: string[],
   *   sessions: object[]}} report
   */
  reportFeed: (report) => {
    // Who is waiting, not just how many — the shell re-expands the HUD only for
    // attention the user has not already seen. A list of anything other than
    // strings travels as `null` rather than as a filtered one: a list missing an
    // entry would let a waiting session pass as new for ever.
    const awaitingIds = report?.awaitingIds;
    const named =
      Array.isArray(awaitingIds) && awaitingIds.every((id) => typeof id === "string")
        ? [...awaitingIds]
        : null;
    ipcRenderer.send("omnicraft:hud-report-feed", {
      readable: boolean(report?.readable),
      exact: boolean(report?.exact),
      stale: boolean(report?.stale),
      truncated: boolean(report?.truncated),
      // Whether the row list accounts for every session in scope. Three-valued
      // like the rest: an unread completeness claim is not a completeness
      // claim, and the shell prunes state on the strength of this one.
      observationComplete: boolean(report?.observationComplete),
      // The SERVER's clock, and the only one an age may be measured against.
      generatedAtMs: number(report?.generatedAtMs),
      active: number(report?.active),
      awaiting: number(report?.awaiting),
      unresolved: number(report?.unresolved),
      awaitingIds: named,
      sessions: sanitizeSessions(report?.sessions),
    });
  },

  /**
   * Subscribe to the shell's own expand/collapse decisions (it auto-expands
   * when attention appears), so the panel renders the state its window is in.
   * Returns an unsubscribe function.
   *
   * @param {(expanded: boolean) => void} callback
   */
  onExpandedChanged: (callback) => {
    const listener = (_event, expanded) => callback(expanded === true);
    ipcRenderer.on("omnicraft:hud-expanded", listener);
    return () => ipcRenderer.removeListener("omnicraft:hud-expanded", listener);
  },
});
