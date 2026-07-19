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
});
