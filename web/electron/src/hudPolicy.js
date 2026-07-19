// The floating HUD's state machine: what the shell does with the window when
// the settings change, when a feed report arrives, when the user toggles the
// menu item, and when the window opens or closes.
//
// It lives here rather than in main.js so it can be driven directly by tests —
// the visibility rules are only worth as much as the wiring that applies them,
// and wiring asserted by grepping main.js for a function name is wiring that
// passes while broken. Everything Electron-shaped arrives injected: the window
// is an adapter (`show`, `hide`, `close`, `setExpanded`), main.js implements it
// over a BrowserWindow.
//
// The one invariant to keep in mind while reading: the shell may only undo what
// the shell did. A HUD the user expanded by hand is theirs, and stays expanded
// long after the attention that happened alongside it is gone.

"use strict";

const { acknowledgeAttention, carryAcknowledged, decideHud } = require("./hudVisibility");

/**
 * @typedef {object} HudWindowHandle
 * @property {() => boolean} isReady Whether the page reached ready-to-show.
 *   Showing before that paints an empty always-on-top rectangle.
 * @property {() => boolean} isVisible
 * @property {() => void} show Show WITHOUT focus (showInactive).
 * @property {() => void} hide
 * @property {() => void} close
 * @property {(expanded: boolean, notifyRenderer: boolean) => void} setExpanded
 *   Apply the footprint; `notifyRenderer` false when the page is the one that
 *   asked (it is already in that state).
 */

/**
 * @param {object} deps
 * @param {() => {enabled: boolean | null, mode: string | null}} deps.readSettings
 * @param {(patch: {enabled?: boolean, mode?: string}) => void} deps.writeSettings
 *   May throw (an unreadable settings.json must not be overwritten).
 * @param {() => HudWindowHandle | null} deps.getWindow
 * @param {() => void} deps.openWindow Create the window; it reports back via
 *   `windowReady()` once it can be shown.
 * @param {(message: string, error: unknown) => void} [deps.onError]
 */
function createHudPolicy({ readSettings, writeSettings, getWindow, openWindow, onError }) {
  const reportError = onError ?? (() => {});

  /** What the window is showing: the pill, or the session list. */
  let expanded = false;
  /** True while THIS expansion is the shell's own doing (attention arrived). */
  let autoExpanded = false;
  /** The renderer's last word on the feed; null means "we have not been told". */
  let report = null;
  /**
   * The attention the user dismissed by collapsing the panel; `null` when they
   * have dismissed nothing. Only a session that is NOT in here re-expands the
   * HUD, so a permission that stays pending stops re-opening what was closed
   * on it.
   */
  let acknowledged = null;
  /** True when the SHELL asked for the pending close (quit, disable, teardown). */
  let shellInitiatedClose = false;

  /** Forget everything tied to one window; the next one re-reads for itself. */
  function reset() {
    expanded = false;
    autoExpanded = false;
    report = null;
    acknowledged = null;
  }

  /**
   * Bring the window in line with the settings and the last report.
   *
   * Hiding is `hide()`, not `close()` — the hidden window keeps polling, which
   * is the only way it can know when to come back. Only the setting being off
   * closes it.
   */
  function apply() {
    const { enabled, mode } = readSettings();
    const win = getWindow();
    if (enabled !== true) {
      // Unknown settings neither open an always-on-top window nor close one the
      // user already has: only an explicit `false` closes.
      autoExpanded = false;
      acknowledged = null;
      if (enabled === false && win) {
        shellInitiatedClose = true;
        win.close();
      }
      return;
    }
    if (!win) {
      openWindow(); // re-enters through windowReady() once the page can show
      return;
    }

    // A dismissal only survives while the feed still shows the same attention:
    // a session it PROVES is no longer waiting drops out, so the same session
    // blocking again later is new attention.
    acknowledged = carryAcknowledged(acknowledged, report);

    const decision = decideHud({ enabled, mode, report, expanded, autoExpanded, acknowledged });
    autoExpanded = decision.autoExpanded;
    if (decision.expanded !== null && decision.expanded !== expanded) {
      expanded = decision.expanded;
      win.setExpanded(expanded, true);
    }
    // Never show a window that hasn't painted: that is the startup flicker the
    // modes exist to avoid — a HUD that should start hidden must never appear
    // first and disappear after.
    if (!win.isReady()) return;
    if (decision.visible) {
      if (!win.isVisible()) win.show();
    } else if (win.isVisible()) {
      win.hide();
    }
  }

  return {
    /** Re-decide from the current settings and the last report. */
    applyPolicy: apply,

    /**
     * The renderer's feed snapshot. Everything unresolved travels as
     * unresolved; the decision is what refuses to call it idle.
     */
    setFeedReport(next) {
      report = next ?? null;
      apply();
    },

    /**
     * The user expanded or collapsed the HUD from its own page. Their choice —
     * so it is explicitly NOT the shell's to undo later.
     *
     * Collapsing also ACKNOWLEDGES the attention on screen: whatever is waiting
     * right now has been seen, and must not re-open the panel on the next poll.
     */
    setUserExpanded(next) {
      expanded = next === true;
      autoExpanded = false;
      acknowledged = expanded ? null : acknowledgeAttention(report);
      const win = getWindow();
      if (win) win.setExpanded(expanded, false);
    },

    /**
     * Menu item: turn the HUD on or off. Keyed on what the user can SEE, not on
     * whether a window object exists — a window hidden by the visibility mode
     * reads as off, so the menu shows it instead of "turning off" something
     * that was never on screen.
     */
    toggle() {
      const win = getWindow();
      const showing = Boolean(win && win.isVisible());
      // Asking to SEE the HUD is asking for a HUD that stays: turning it on
      // under a mode that hides it would put the window up and let the next
      // report take it away. So the mode gives way too. Losing the stored mode
      // is the smaller loss — it is right there in Settings → Desktop → HUD,
      // visible and editable, where a HUD that flashes and vanishes explains
      // itself nowhere.
      const patch = showing ? { enabled: false } : { enabled: true, mode: "always" };
      try {
        writeSettings(patch);
      } catch (err) {
        reportError("could not persist the HUD toggle", err);
        return;
      }
      apply();
      const after = getWindow();
      if (!showing && after && after.isReady() && !after.isVisible()) after.show();
    },

    /** The window painted and can be shown. */
    windowReady() {
      apply();
    },

    /**
     * The SHELL is about to take the window down (settings off, the last shell
     * window going away, the app quitting). Marks the next close as not the
     * user's, so quitting never persists the HUD as "off".
     */
    shellClosing() {
      shellInitiatedClose = true;
    },

    /**
     * The window is gone.
     *
     * A close the shell did not announce is the user closing the HUD, and that
     * has to persist — otherwise the next policy pass (a feed report, the next
     * launch) reopens a window the user just dismissed.
     *
     * @param {{userInitiated?: boolean}} [cause] Overrides the flag; main.js
     *   leaves it to `shellClosing()`.
     */
    windowClosed(cause) {
      const userInitiated = cause?.userInitiated ?? !shellInitiatedClose;
      shellInitiatedClose = false;
      reset();
      if (!userInitiated) return;
      try {
        writeSettings({ enabled: false });
      } catch (err) {
        reportError("could not persist the HUD being closed", err);
      }
    },

    /** Test/introspection seam: the state the decisions run on. */
    state() {
      return { expanded, autoExpanded, report, acknowledged };
    },
  };
}

module.exports = { createHudPolicy };
