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
   * Plain numbers/booleans so the payload survives structured cloning. Fields
   * carry their own uncertainty: `readable` false or `exact` false means the
   * counts are not an answer, and the shell must not read them as "idle".
   *
   * @param {{readable: boolean, exact: boolean, stale: boolean,
   *   active: number, awaiting: number, unresolved: number}} report
   */
  reportFeed: (report) => {
    ipcRenderer.send("omnicraft:hud-report-feed", {
      readable: report?.readable === true,
      exact: report?.exact === true,
      stale: report?.stale === true,
      active: Number(report?.active),
      awaiting: Number(report?.awaiting),
      unresolved: Number(report?.unresolved),
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
