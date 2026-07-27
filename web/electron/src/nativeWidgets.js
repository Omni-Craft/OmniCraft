// The native widgets (the floating panels) as a child process of the shell.
//
// Same shape as nativeIsland.js, and for the same reason: they are a separate
// macOS app, not windows, so the shell owns only their LIFETIME — start them
// when the setting is on, stop them on quit — and they talk to the OmniCraft
// server directly for everything else.
//
// Unlike the island, WHICH panels are open is part of the launch: the app
// takes `--widget <nome>` per panel. Changing the selection therefore restarts
// the process, which is why the controller owns that too.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

/** Where the bundle lives inside a packaged app, relative to resourcesPath. */
const PACKAGED_RELATIVE = path.join("native", "OmniCraftWidgets.app");

/** Where `scripts/make-app.sh` leaves it in a checkout, relative to repo root. */
const CHECKOUT_RELATIVE = path.join(
  "native",
  "macos",
  "OmniCraftWidgets",
  ".build",
  "OmniCraftWidgets.app",
);

/** The panels the app knows how to open. */
const WIDGET_KINDS = [
  "board",
  "transcript",
  "ferramentas",
  "subagentes",
  "uso",
  "tarefas",
  "servidores",
  "rotas",
];

/**
 * Locate the widgets bundle.
 *
 * Returns the reason it is unavailable rather than throwing: "not built yet"
 * is an ordinary state in a checkout, and the settings page has to be able to
 * SAY so instead of offering a switch that silently does nothing.
 *
 * @param {{resourcesPath?: string, appPath?: string, platform?: string}} env
 * @returns {{path: string} | {path: null, reason: string}}
 */
function resolveWidgetsApp(env = {}) {
  const platform = env.platform ?? process.platform;
  if (platform !== "darwin") {
    return { path: null, reason: "os widgets só existem no macOS" };
  }
  const candidates = [];
  if (env.resourcesPath) candidates.push(path.join(env.resourcesPath, PACKAGED_RELATIVE));
  if (env.appPath) {
    // appPath is web/electron in a checkout and the asar in a packaged build;
    // the repo root is two levels up from the former.
    candidates.push(path.join(env.appPath, "..", "..", CHECKOUT_RELATIVE));
  }
  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (fs.existsSync(path.join(resolved, "Contents", "MacOS", "OmniCraftWidgets"))) {
      return { path: resolved };
    }
  }
  return {
    path: null,
    reason:
      "os widgets ainda não foram construídos (native/macos/OmniCraftWidgets/scripts/make-app.sh)",
  };
}

/**
 * Owns the widgets process: at most one, never orphaned.
 */
class NativeWidgetsController {
  /**
   * @param {{resolve?: typeof resolveWidgetsApp, spawnFn?: typeof spawn,
   *          env?: object, serverUrl?: () => string | null,
   *          onWarn?: (message: string) => void}} deps
   */
  constructor(deps = {}) {
    this._resolve = deps.resolve ?? resolveWidgetsApp;
    this._spawn = deps.spawnFn ?? spawn;
    this._env = deps.env ?? {};
    this._serverUrl = deps.serverUrl ?? (() => null);
    this._warn = deps.onWarn ?? (() => {});
    this._child = null;
    /** The panels the running process was started with. */
    this._panels = [];
  }

  /** @returns {boolean} Whether the widgets process is running right now. */
  get running() {
    return this._child !== null;
  }

  /**
   * Start the widgets with the given panels, if not already up with them.
   *
   * @param {string[]} panels
   * @returns {{started: boolean, reason?: string}}
   */
  start(panels = []) {
    const wanted = panels.filter((panel) => WIDGET_KINDS.includes(panel));
    if (!wanted.length) {
      // No panel means nothing to draw: a process with no window is a process
      // the user cannot see, quit, or benefit from.
      this.stop();
      return { started: false, reason: "nenhum painel escolhido" };
    }
    if (this._child && sameSet(this._panels, wanted)) return { started: true };
    this.stop();

    const found = this._resolve(this._env);
    if (!found.path) {
      this._warn(`widgets não iniciados: ${found.reason}`);
      return { started: false, reason: found.reason };
    }
    const binary = path.join(found.path, "Contents", "MacOS", "OmniCraftWidgets");
    const args = ["--live"];
    const url = this._serverUrl();
    if (url) args.push("-OmniCraftFeedBaseURL", url);
    for (const panel of wanted) args.push("--widget", panel);

    let child;
    try {
      // Spawned directly rather than via `open`, for the same reason as the
      // island: `open` hands the process to launchd, which would outlive the
      // shell — the exact orphan this class exists to avoid.
      child = this._spawn(binary, args, { stdio: "ignore", detached: false });
    } catch (error) {
      const reason = error?.message ?? String(error);
      this._warn(`widgets não iniciados: ${reason}`);
      return { started: false, reason };
    }
    child.on("exit", () => {
      if (this._child === child) this._child = null;
    });
    child.on("error", (error) => {
      this._warn(`widgets terminaram com erro: ${error?.message ?? error}`);
      if (this._child === child) this._child = null;
    });
    this._child = child;
    this._panels = wanted;
    return { started: true };
  }

  /** Stop the widgets if running. Safe to call when they are not. */
  stop() {
    const child = this._child;
    if (!child) return;
    this._child = null;
    this._panels = [];
    try {
      child.kill();
    } catch (error) {
      this._warn(`widgets não encerraram: ${error?.message ?? error}`);
    }
  }

  /**
   * Bring the process in line with the settings.
   *
   * @param {boolean} enabled
   * @param {string[]} panels
   */
  apply(enabled, panels) {
    if (enabled) return this.start(panels);
    this.stop();
    return undefined;
  }
}

/** Whether two panel selections are the same set, order aside. */
function sameSet(a, b) {
  return a.length === b.length && a.every((item) => b.includes(item));
}

module.exports = {
  resolveWidgetsApp,
  NativeWidgetsController,
  WIDGET_KINDS,
  PACKAGED_RELATIVE,
  CHECKOUT_RELATIVE,
};
