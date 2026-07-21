// The native island (the notch HUD) as a child process of the desktop shell.
//
// It is a separate macOS app, not a window: it has to live above the menu bar,
// which an Electron window cannot do. So the shell only owns its LIFETIME —
// start it when the setting is on, stop it on quit — and the island talks to
// the OmniCraft server directly for everything else.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

/** Where the bundle lives inside a packaged app, relative to resourcesPath. */
const PACKAGED_RELATIVE = path.join("native", "OmniCraftNotch.app");

/** Where `scripts/make-app.sh` leaves it in a checkout, relative to repo root. */
const CHECKOUT_RELATIVE = path.join(
  "native",
  "macos",
  "OmniCraftNotch",
  ".build",
  "OmniCraftNotch.app",
);

/**
 * Locate the island bundle.
 *
 * Returns the reason it is unavailable rather than throwing: "not built yet"
 * is an ordinary state in a checkout, and the settings page has to be able to
 * SAY so instead of offering a switch that silently does nothing.
 *
 * @param {{resourcesPath?: string, appPath?: string, platform?: string}} env
 * @returns {{path: string} | {path: null, reason: string}}
 */
function resolveIslandApp(env = {}) {
  const platform = env.platform ?? process.platform;
  if (platform !== "darwin") {
    return { path: null, reason: "a ilha só existe no macOS" };
  }
  const candidates = [];
  if (env.resourcesPath) candidates.push(path.join(env.resourcesPath, PACKAGED_RELATIVE));
  if (env.appPath) {
    // From web/electron up to the repo root.
    candidates.push(path.join(env.appPath, "..", "..", CHECKOUT_RELATIVE));
  }
  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (fs.existsSync(path.join(resolved, "Contents", "MacOS", "OmniCraftNotch"))) {
      return { path: resolved };
    }
  }
  return {
    path: null,
    reason: "a ilha ainda não foi construída (native/macos/OmniCraftNotch/scripts/make-app.sh)",
  };
}

/**
 * Owns the island process: at most one, never orphaned.
 */
class NativeIslandController {
  /**
   * @param {{resolve?: typeof resolveIslandApp, spawnFn?: typeof spawn,
   *          env?: object, onWarn?: (message: string) => void}} deps
   */
  constructor(deps = {}) {
    this._resolve = deps.resolve ?? resolveIslandApp;
    this._spawn = deps.spawnFn ?? spawn;
    this._env = deps.env ?? {};
    this._warn = deps.onWarn ?? (() => {});
    this._child = null;
  }

  /** @returns {boolean} Whether the island process is running right now. */
  get running() {
    return this._child !== null;
  }

  /**
   * Start the island if it is not already up.
   *
   * @returns {{started: boolean, reason?: string}}
   */
  start() {
    if (this._child) return { started: true };
    const found = this._resolve(this._env);
    if (!found.path) {
      this._warn(`ilha não iniciada: ${found.reason}`);
      return { started: false, reason: found.reason };
    }
    const binary = path.join(found.path, "Contents", "MacOS", "OmniCraftNotch");
    let child;
    try {
      // Spawned directly rather than via `open`: `open` returns immediately and
      // hands the process to launchd, which would leave the island running
      // after the shell quits — the exact orphan this class exists to avoid.
      child = this._spawn(binary, [], { stdio: "ignore", detached: false });
    } catch (error) {
      const reason = error?.message ?? String(error);
      this._warn(`ilha não iniciada: ${reason}`);
      return { started: false, reason };
    }
    child.on("exit", () => {
      if (this._child === child) this._child = null;
    });
    child.on("error", (error) => {
      this._warn(`ilha terminou com erro: ${error?.message ?? error}`);
      if (this._child === child) this._child = null;
    });
    this._child = child;
    return { started: true };
  }

  /** Stop the island if it is running. Safe to call when it is not. */
  stop() {
    const child = this._child;
    if (!child) return;
    this._child = null;
    try {
      child.kill();
    } catch (error) {
      this._warn(`ilha não encerrou: ${error?.message ?? error}`);
    }
  }

  /**
   * Bring the process in line with the setting.
   *
   * @param {boolean} enabled
   * @returns {{started: boolean, reason?: string} | undefined}
   */
  apply(enabled) {
    if (enabled) return this.start();
    this.stop();
    return undefined;
  }
}

module.exports = { resolveIslandApp, NativeIslandController, PACKAGED_RELATIVE, CHECKOUT_RELATIVE };
