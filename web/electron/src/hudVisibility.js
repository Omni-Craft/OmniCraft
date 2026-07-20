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

const {
  DEFAULT_BUDGET_THRESHOLD,
  HUD_NOTIFICATION_CATEGORIES,
  isValidBudgetThreshold,
  parseQuietTime,
} = require("./hudNotifications");

/** The three visibility modes offered in Settings → Desktop → HUD. */
const HUD_VISIBILITY_MODES = ["always", "hide-when-idle", "attention-only"];

/** Mode a never-configured install runs in: no hiding without being asked. */
const DEFAULT_HUD_VISIBILITY = "always";

/**
 * Notification preferences a never-configured install runs on: every category
 * on, no quiet hours, the documented budget threshold.
 *
 * Everything defaults to ON because the alternative is a monitor that watches
 * in silence — the user turned the HUD on to be told things.
 */
const DEFAULT_HUD_NOTIFICATIONS = Object.freeze({
  permission: true,
  budget: true,
  stuck: true,
  completion: true,
  quietFrom: null,
  quietTo: null,
  budgetThreshold: DEFAULT_BUDGET_THRESHOLD,
});

/** What an unreadable settings blob reads as — never "off". */
const HUD_SETTINGS_UNREADABLE = Object.freeze({
  readable: false,
  enabled: null,
  mode: null,
  notifications: null,
  sound: null,
});

/** Whether a quiet-hours endpoint was left unset at all. */
function unsetQuietTime(value) {
  return value === undefined || value === null;
}

/**
 * Read the notification preferences out of a `hud` blob, or `null` when they
 * cannot be interpreted.
 *
 * The rule is the one `mode` already follows, and following it is what keeps
 * this backwards-compatible: a field that is ABSENT is a settings.json written
 * by a build that had no such field, which is a fact and reads as the default.
 * A field that is PRESENT but malformed is a file we cannot interpret, and the
 * whole read degrades to unknown rather than to a set of defaults the user
 * never chose.
 *
 * A key this build does not KNOW is ignored instead: it comes from a newer
 * build, and it makes none of the fields here wrong. (Writing one is another
 * matter — hudIpc refuses unknown keys, so this build never creates them.)
 *
 * @param {unknown} value `hud.notifications`.
 * @returns {object | null}
 */
function readNotificationSettings(value) {
  if (value === undefined) return { ...DEFAULT_HUD_NOTIFICATIONS };
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const prefs = { ...DEFAULT_HUD_NOTIFICATIONS };

  for (const category of HUD_NOTIFICATION_CATEGORIES) {
    const enabled = value[category];
    if (enabled === undefined) continue;
    if (typeof enabled !== "boolean") return null;
    prefs[category] = enabled;
  }

  // Both ends or neither: half a range names no span, and inventing the other
  // end would invent a silence nobody asked for.
  const { quietFrom, quietTo } = value;
  if (unsetQuietTime(quietFrom) !== unsetQuietTime(quietTo)) return null;
  if (!unsetQuietTime(quietFrom)) {
    if (parseQuietTime(quietFrom) === null || parseQuietTime(quietTo) === null) return null;
    prefs.quietFrom = quietFrom;
    prefs.quietTo = quietTo;
  }

  if (value.budgetThreshold !== undefined) {
    if (!isValidBudgetThreshold(value.budgetThreshold)) return null;
    prefs.budgetThreshold = value.budgetThreshold;
  }
  return prefs;
}

/**
 * Whether the notification sound is on, or `null` when the stored value cannot
 * be read.
 *
 * This is the app-wide `notification_sound_enabled` — the same preference the
 * native Notifications menu toggles and every desktop toast already obeys.
 * Surfacing it here rather than adding a HUD-only copy keeps one answer to "is
 * there sound", instead of two switches that can disagree.
 *
 * Absent is a FACT: the sound is opt-in, so a fresh install is silent.
 */
function readNotificationSound(value) {
  if (value === undefined) return false;
  return typeof value === "boolean" ? value : null;
}

/**
 * Read the `hud` blob out of a parsed settings.json.
 *
 * Absent means never configured, which is a FACT (the HUD does not open on its
 * own today) and reads as `enabled: false`. Present-but-malformed — a
 * hand-edited file, a mode this build doesn't know — is NOT a fact and reads
 * unreadable, so the UI can say "desconhecido" rather than claim it is off.
 *
 * @param {unknown} settings Parsed settings.json, or null when unreadable.
 * @returns {{readable: boolean, enabled: boolean | null, mode: string | null,
 *   notifications: object | null, sound: boolean | null}}
 */
function readHudSettings(settings) {
  if (settings === null || typeof settings !== "object" || Array.isArray(settings)) {
    return HUD_SETTINGS_UNREADABLE;
  }
  const sound = readNotificationSound(settings.notification_sound_enabled);
  if (sound === null) return HUD_SETTINGS_UNREADABLE;
  const hud = settings.hud;
  if (hud === undefined) {
    return {
      readable: true,
      enabled: false,
      mode: DEFAULT_HUD_VISIBILITY,
      notifications: { ...DEFAULT_HUD_NOTIFICATIONS },
      sound,
    };
  }
  if (hud === null || typeof hud !== "object" || Array.isArray(hud)) return HUD_SETTINGS_UNREADABLE;
  if (typeof hud.enabled !== "boolean") return HUD_SETTINGS_UNREADABLE;
  // A missing mode on an otherwise valid blob is the default; a mode that is
  // present but unrecognized is a file we cannot interpret.
  const mode = hud.mode === undefined ? DEFAULT_HUD_VISIBILITY : hud.mode;
  if (!HUD_VISIBILITY_MODES.includes(mode)) return HUD_SETTINGS_UNREADABLE;
  const notifications = readNotificationSettings(hud.notifications);
  if (notifications === null) return HUD_SETTINGS_UNREADABLE;
  return { readable: true, enabled: hud.enabled, mode, notifications, sound };
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
 * The merge is TWO levels deep, and has to be. The notification preferences are
 * a sub-object the UI patches one field at a time — one toggle, one threshold —
 * so a shallow spread would replace the whole sub-object and switch off the
 * three categories the user did not touch.
 *
 * The sound is deliberately NOT part of the hud blob: it is the app-wide
 * `notification_sound_enabled`, shared with the native Notifications menu, and
 * a patch writes that same top-level key.
 *
 * @param {{ok: boolean, settings: object | null}} read Result of reading
 *   settings.json, with "absent" (first launch) distinct from "unreadable".
 * @param {{enabled?: boolean, mode?: string, notifications?: object,
 *   sound?: boolean}} patch
 * @returns {{settings: object, hud: object, sound: boolean}}
 */
function mergeHudSettings(read, patch) {
  if (!read || read.ok !== true || read.settings === null || typeof read.settings !== "object") {
    throw new Error("settings.json could not be read; refusing to overwrite it");
  }
  const current = readHudSettings(read.settings);
  const base = current.readable
    ? { enabled: current.enabled, mode: current.mode, notifications: current.notifications }
    : {
        enabled: false,
        mode: DEFAULT_HUD_VISIBILITY,
        notifications: { ...DEFAULT_HUD_NOTIFICATIONS },
      };
  const { notifications: notificationsPatch, sound, ...top } = patch ?? {};
  const hud = {
    ...base,
    ...top,
    notifications: { ...base.notifications, ...(notificationsPatch ?? {}) },
  };
  const settings = { ...read.settings, hud };
  if (sound !== undefined) settings.notification_sound_enabled = sound;
  return { settings, hud, sound: settings.notification_sound_enabled === true };
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
 * @typedef {{named: true, ids: string[]} | {named: false}} Dismissal
 *   What the user dismissed by collapsing the panel: the sessions they saw
 *   waiting, or the fact that they dismissed something the feed could not name.
 *
 *   The unnameable case is its own STATE, not a reserved id: a magic string
 *   would live in the same namespace as the ids off the wire, so a session that
 *   happened to be called that would read as "the user dismissed something
 *   unnameable" and swallow the next new prompt.
 */

/** The user dismissed attention the feed could not name. */
const UNNAMED_DISMISSAL = Object.freeze({ named: false });

/** Whether a value is a dismissal at all — `null` is "nothing was dismissed". */
function isDismissal(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/**
 * The ids of the sessions the report PROVES are waiting on a human, or `null`
 * when it proves nothing.
 *
 * This is the mirror image of the visibility rule, and on purpose. To SHOW the
 * window, not-knowing is enough — an unresolved feed stays on screen. To REOPEN
 * a panel the user just collapsed, not-knowing is NOT enough: a list we cannot
 * fully read cannot tell new attention from the attention already dismissed,
 * and guessing means re-expanding the HUD on every poll.
 *
 * So the identity only counts when the snapshot is readable, exact and fresh,
 * the ids are really there, and they ACCOUNT for every session the tallies say
 * is waiting — one waiting session the list cannot name is one we would fail to
 * recognize the next time around.
 *
 * @param {unknown} report
 * @returns {string[] | null}
 */
function awaitingSignature(report) {
  if (report === null || typeof report !== "object") return null;
  if (report.readable !== true || report.exact !== true || report.stale === true) return null;
  if (report.unresolved !== 0) return null;
  const ids = report.awaitingIds;
  if (!Array.isArray(ids) || !ids.every((id) => typeof id === "string" && id.length > 0)) {
    return null;
  }
  if (new Set(ids).size !== ids.length || ids.length !== report.awaiting) return null;
  return ids;
}

/**
 * The attention the user dismisses by collapsing the panel. When the feed
 * cannot name it, the dismissal is recorded as unnameable rather than as
 * nothing — collapsing under a stale feed still means "I have seen this".
 *
 * @param {unknown} report
 * @returns {Dismissal}
 */
function acknowledgeAttention(report) {
  const ids = awaitingSignature(report);
  return ids === null ? UNNAMED_DISMISSAL : { named: true, ids: [...ids] };
}

/**
 * Carry a dismissal onto the next report: an id the feed proves is no longer
 * waiting drops off, so attention that really went away and came back counts as
 * new. A feed that proves nothing changes nothing — that a session vanished
 * from a list we could not read is not evidence it was answered.
 *
 * Idempotent, so applying it before `decideHud` and inside it is the same
 * thing.
 *
 * @param {Dismissal | null} acknowledged
 * @param {unknown} report
 * @returns {Dismissal | null}
 */
function carryAcknowledged(acknowledged, report) {
  if (!isDismissal(acknowledged)) return null;
  const ids = awaitingSignature(report);
  if (ids === null) return acknowledged;
  // The user dismissed something we could not name; now that we can, that is
  // what they dismissed.
  if (acknowledged.named !== true) return { named: true, ids: [...ids] };
  return { named: true, ids: acknowledged.ids.filter((id) => ids.includes(id)) };
}

/**
 * Whether the report names attention the user has NOT already dismissed —
 * the only thing allowed to re-expand a panel they collapsed.
 *
 * @param {unknown} report
 * @param {Dismissal | null} acknowledged `null` = the user dismissed nothing.
 * @returns {boolean}
 */
function hasNewAttention(report, acknowledged) {
  if (!isDismissal(acknowledged)) return true;
  const ids = awaitingSignature(report);
  // A dismissal stands until the feed can prove what is waiting now — an
  // unnameable one included (`carryAcknowledged` gives it names first).
  if (ids === null || acknowledged.named !== true) return false;
  return ids.some((id) => !acknowledged.ids.includes(id));
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
 * `acknowledged` is the attention the user dismissed by collapsing the panel:
 * persistent attention re-expands nothing, only a session they have not seen
 * does.
 *
 * @param {{enabled: boolean | null, mode: string | null, report: unknown,
 *   expanded?: boolean, autoExpanded?: boolean,
 *   acknowledged?: Dismissal | null}} input
 * @returns {{visible: boolean, expanded: boolean | null, autoExpanded: boolean}}
 */
function decideHud({
  enabled,
  mode,
  report,
  expanded = false,
  autoExpanded = false,
  acknowledged = null,
}) {
  // Only an explicit `true` opens the HUD: unknown settings must not silently
  // put an always-on-top window on the user's screen.
  if (enabled !== true) return { visible: false, expanded: false, autoExpanded: false };

  const { attention, idleCertain } = summarizeFeedReport(report);
  if (attention) {
    // Attention only OWNS the expansion it actually caused. On a HUD the user
    // already opened by hand there is nothing to expand, and claiming it would
    // hand the shell a manual expansion to collapse later.
    if (expanded) return { visible: true, expanded: null, autoExpanded };
    // A pending permission stays pending: without this, every poll would
    // re-open the panel the user just closed on it.
    if (!hasNewAttention(report, carryAcknowledged(acknowledged, report))) {
      return { visible: true, expanded: null, autoExpanded };
    }
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
  DEFAULT_HUD_NOTIFICATIONS,
  UNNAMED_DISMISSAL,
  readHudSettings,
  mergeHudSettings,
  summarizeFeedReport,
  awaitingSignature,
  acknowledgeAttention,
  carryAcknowledged,
  hasNewAttention,
  decideHud,
};
