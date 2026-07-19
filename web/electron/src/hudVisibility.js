// Reading of the HUD's persisted settings, and the decision the shell makes
// from them plus the feed: should the HUD window be on screen right now, and
// should it be expanded?
//
// Pure and dependency-free so `node --test` can exercise every branch without
// an Electron runtime. main.js owns the window; this owns the judgement.
//
// One rule shapes all of it: **an unresolved feed is not an idle one.** The
// counts arrive from a monitor that documents its own failures — a read that
// never landed, tallies that are a floor rather than a total, a snapshot that
// stopped refreshing. Treating any of those as "nothing is running" would hide
// the monitor exactly when it has stopped being able to tell you it shouldn't.
// So "idle" has to be PROVEN (fresh, exact, all-zero); everything else keeps
// the HUD visible, whatever the mode says.

"use strict";

/** The three visibility modes offered in Settings → Desktop → HUD. */
const HUD_VISIBILITY_MODES = ["always", "hide-when-idle", "attention-only"];

/** Mode a never-configured install runs in: no hiding without being asked. */
const DEFAULT_HUD_VISIBILITY = "always";

/** What an unreadable settings blob reads as — never "off". */
const HUD_SETTINGS_UNREADABLE = Object.freeze({ readable: false, enabled: null, mode: null });

/**
 * Read the `hud` blob out of a parsed settings.json.
 *
 * Absent means never configured, which is a FACT (the HUD does not open on its
 * own today) and reads as `enabled: false`. Present-but-malformed — a
 * hand-edited file, a mode this build doesn't know — is NOT a fact and reads
 * unreadable, so the UI can say "desconhecido" rather than claim it is off.
 *
 * @param {unknown} settings Parsed settings.json, or null when unreadable.
 * @returns {{readable: boolean, enabled: boolean | null, mode: string | null}}
 */
function readHudSettings(settings) {
  if (settings === null || typeof settings !== "object" || Array.isArray(settings)) {
    return HUD_SETTINGS_UNREADABLE;
  }
  const hud = settings.hud;
  if (hud === undefined) return { readable: true, enabled: false, mode: DEFAULT_HUD_VISIBILITY };
  if (hud === null || typeof hud !== "object" || Array.isArray(hud)) return HUD_SETTINGS_UNREADABLE;
  if (typeof hud.enabled !== "boolean") return HUD_SETTINGS_UNREADABLE;
  // A missing mode on an otherwise valid blob is the default; a mode that is
  // present but unrecognized is a file we cannot interpret.
  const mode = hud.mode === undefined ? DEFAULT_HUD_VISIBILITY : hud.mode;
  if (!HUD_VISIBILITY_MODES.includes(mode)) return HUD_SETTINGS_UNREADABLE;
  return { readable: true, enabled: hud.enabled, mode };
}

/**
 * Merge a HUD patch into a settings.json READ, returning the whole settings
 * object to persist plus the resulting hud blob.
 *
 * THROWS when the read failed. settings.json holds the saved server, the
 * recents and every other preference; writing after a failed read would write
 * over contents we never parsed, losing all of it to save one boolean. Refusing
 * leaves the file intact for the user to fix, and the caller surfaces the
 * failure (Settings already renders "não pôde ser salva").
 *
 * A hud blob that is malformed inside a READABLE file is different: there is
 * nothing in it to preserve, so it is replaced.
 *
 * @param {{ok: boolean, settings: object | null}} read Result of reading
 *   settings.json, with "absent" (first launch) distinct from "unreadable".
 * @param {{enabled?: boolean, mode?: string}} patch
 * @returns {{settings: object, hud: {enabled: boolean, mode: string}}}
 */
function mergeHudSettings(read, patch) {
  if (!read || read.ok !== true || read.settings === null || typeof read.settings !== "object") {
    throw new Error("settings.json could not be read; refusing to overwrite it");
  }
  const current = readHudSettings(read.settings);
  const base = current.readable
    ? { enabled: current.enabled, mode: current.mode }
    : { enabled: false, mode: DEFAULT_HUD_VISIBILITY };
  const hud = { ...base, ...patch };
  return { settings: { ...read.settings, hud }, hud };
}

/**
 * What the HUD's renderer told us about the feed, reduced to the two questions
 * visibility turns on. Anything malformed lands in "not certain", never in
 * "idle".
 *
 * `attention` is true on a floor or a stale snapshot too: "at least one
 * session was blocked" stays a reason to be on screen even when the number is
 * no longer current. `idleCertain` is the opposite — it demands a snapshot
 * that is fresh, exact, and zero on every bucket, including the sessions the
 * feed could not resolve or had to omit.
 *
 * @param {unknown} report
 * @returns {{attention: boolean, idleCertain: boolean}}
 */
function summarizeFeedReport(report) {
  if (report === null || typeof report !== "object")
    return { attention: false, idleCertain: false };
  const count = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);
  const awaiting = count(report.awaiting);
  const active = count(report.active);
  const unresolved = count(report.unresolved);
  const attention = awaiting !== null && awaiting > 0;
  const idleCertain =
    report.readable === true &&
    report.exact === true &&
    report.stale !== true &&
    active === 0 &&
    awaiting === 0 &&
    unresolved === 0;
  return { attention, idleCertain };
}

/**
 * Decide the HUD's window state for one feed report.
 *
 * `expanded: null` means "leave it as the user (or the last decision) left it"
 * — the shell must not fight a manual expand, and must not collapse a HUD it
 * auto-expanded while the reason for it is merely unknown. `autoExpanded`
 * tracks whether the CURRENT expansion was the shell's doing, so the return to
 * collapsed only ever undoes an auto-expand. `expanded` is what the window is
 * showing right now, which is what tells attention whether it CAUSED the
 * expansion it is about to claim.
 *
 * @param {{enabled: boolean | null, mode: string | null, report: unknown,
 *   expanded?: boolean, autoExpanded?: boolean}} input
 * @returns {{visible: boolean, expanded: boolean | null, autoExpanded: boolean}}
 */
function decideHud({ enabled, mode, report, expanded = false, autoExpanded = false }) {
  // Only an explicit `true` opens the HUD: unknown settings must not silently
  // put an always-on-top window on the user's screen.
  if (enabled !== true) return { visible: false, expanded: false, autoExpanded: false };

  const { attention, idleCertain } = summarizeFeedReport(report);
  if (attention) {
    // Attention only OWNS the expansion it actually caused. On a HUD the user
    // already opened by hand there is nothing to expand, and claiming it would
    // hand the shell a manual expansion to collapse later.
    if (expanded) return { visible: true, expanded: null, autoExpanded };
    return { visible: true, expanded: true, autoExpanded: true };
  }

  const effectiveMode = HUD_VISIBILITY_MODES.includes(mode) ? mode : DEFAULT_HUD_VISIBILITY;

  // Attention is neither present nor ruled out (unreadable, floor, or stale
  // counts). Stay on screen whatever the mode, and keep the current expansion:
  // nothing here proves the attention that expanded it is gone.
  if (!idleCertain && !isRestingCertain(report)) {
    return { visible: true, expanded: null, autoExpanded };
  }

  // Certain: nothing is waiting on a human. Undo an auto-expand (and only an
  // auto-expand — a manual one is the user's).
  const nextExpanded = autoExpanded ? false : null;
  if (effectiveMode === "attention-only") {
    return { visible: false, expanded: nextExpanded, autoExpanded: false };
  }
  if (effectiveMode === "hide-when-idle") {
    return { visible: !idleCertain, expanded: nextExpanded, autoExpanded: false };
  }
  return { visible: true, expanded: nextExpanded, autoExpanded: false };
}

/**
 * Whether the report PROVES no session is waiting on a human — the weaker
 * cousin of `idleCertain`, which also demands nothing be running. Work in
 * flight with a fresh, exact, zero-awaiting count is a settled answer for
 * "attention-only": there is nothing to attend to.
 *
 * @param {unknown} report
 * @returns {boolean}
 */
function isRestingCertain(report) {
  if (report === null || typeof report !== "object") return false;
  return (
    report.readable === true &&
    report.exact === true &&
    report.stale !== true &&
    report.awaiting === 0 &&
    report.unresolved === 0
  );
}

module.exports = {
  HUD_VISIBILITY_MODES,
  DEFAULT_HUD_VISIBILITY,
  readHudSettings,
  mergeHudSettings,
  summarizeFeedReport,
  decideHud,
};
