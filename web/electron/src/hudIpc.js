// The floating HUD's IPC surface: the two messages its own page may send, and
// the two calls the SPA's Settings section makes.
//
// Extracted from main.js so the wiring can be DRIVEN by tests — a fake ipcMain
// records the handlers, the tests invoke them with trusted and untrusted
// senders and assert what actually happened. Grepping main.js for a function
// name proves the text is there, not that the message reaches the policy.
//
// Every dependency is injected: `policy` is the HUD state machine
// (src/hudPolicy.js), `getHudWebContents` returns the live HUD's webContents (or
// null), and `isPinnedOriginSender` is main.js's own trust gate for SPA pages.

"use strict";

const { HUD_VISIBILITY_MODES } = require("./hudVisibility");

/**
 * @param {object} deps
 * @param {{on: Function, handle: Function}} deps.ipcMain
 * @param {ReturnType<typeof import("./hudPolicy").createHudPolicy>} deps.policy
 * @param {() => unknown} deps.getHudWebContents The live HUD's webContents, or
 *   null when no HUD is open.
 * @param {(event: unknown) => boolean} deps.isPinnedOriginSender
 * @param {() => {readable: boolean, enabled: boolean | null, mode: string | null}} deps.readSettings
 * @param {(patch: {enabled?: boolean, mode?: string}) => void} deps.writeSettings
 *   Throws when settings.json is present but unreadable — the rejection is the
 *   point: Settings renders "não pôde ser salva" instead of a stale value.
 * @param {(message: string) => void} [deps.onWarn]
 */
function registerHudIpc({
  ipcMain,
  policy,
  getHudWebContents,
  isPinnedOriginSender,
  readSettings,
  writeSettings,
  onWarn,
}) {
  const warn = onWarn ?? (() => {});

  /**
   * Only the live HUD's own page may drive its window. Anything else — another
   * renderer, a stale webContents from a closed HUD — is dropped: these
   * messages move an always-on-top window around.
   */
  const isHudSender = (event) => {
    const hud = getHudWebContents();
    return hud !== null && hud !== undefined && event?.sender === hud;
  };

  // HUD → collapse / expand. The message carries INTENT only; the bounds for
  // each state are chosen by the shell, never sent by the renderer — the HUD's
  // page is server-controlled, and an always-on-top window that could be
  // resized from the page could be grown to cover the screen.
  ipcMain.on("omnicraft:hud-set-expanded", (event, expanded) => {
    if (!isHudSender(event)) {
      warn("hud-set-expanded from untrusted sender dropped");
      return;
    }
    // A hand-driven toggle is the user's: the shell must not later collapse
    // (or keep expanded) a state it did not choose.
    policy.setUserExpanded(expanded === true);
  });

  // HUD → what the feed says right now, from the only renderer that polls it.
  // The payload is read defensively downstream (summarizeFeedReport): a field
  // that isn't there or isn't a number lands in "unresolved", never "idle".
  ipcMain.on("omnicraft:hud-report-feed", (event, report) => {
    if (!isHudSender(event)) {
      warn("hud-report-feed from untrusted sender dropped");
      return;
    }
    policy.setFeedReport(report ?? null);
  });

  // SPA (Settings → Desktop → HUD) → the persisted settings. Carries the
  // readable flag so the page can say "desconhecido" rather than render an
  // unread setting as "off".
  ipcMain.handle("omnicraft:hud-get-settings", (event) => {
    if (!isPinnedOriginSender(event)) {
      warn("hud-get-settings from untrusted sender dropped");
      return null;
    }
    return readSettings();
  });

  // SPA → change the on/off state or the visibility mode, and apply it
  // immediately. Validated here, not just in the page: only the documented
  // shapes are ever persisted, so a bad value can't land as a blob the next
  // launch reads back as unreadable.
  ipcMain.handle("omnicraft:hud-set-settings", (event, patch) => {
    if (!isPinnedOriginSender(event)) {
      throw new Error("hud-set-settings is only available to a connected server page");
    }
    const next = {};
    if (patch?.enabled !== undefined) {
      if (typeof patch.enabled !== "boolean") throw new Error("hud enabled must be a boolean");
      next.enabled = patch.enabled;
    }
    if (patch?.mode !== undefined) {
      if (!HUD_VISIBILITY_MODES.includes(patch.mode))
        throw new Error("unknown hud visibility mode");
      next.mode = patch.mode;
    }
    // Throws through to the renderer when the file can't be read — the write is
    // refused rather than clobbering settings we never parsed.
    writeSettings(next);
    policy.applyPolicy();
    return readSettings();
  });
}

module.exports = { registerHudIpc };
