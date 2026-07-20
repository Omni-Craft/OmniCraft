// Tests for the floating HUD's visibility decision (src/hudVisibility.js), run
// with `node --test` (no extra deps).
//
// The bug class this guards is one specific lie: reading a feed the shell
// could NOT resolve as "nothing is happening" and hiding the monitor on the
// strength of it. So most of what's here is the negative space around "idle" —
// an unreadable feed, a floor instead of a total, a stale snapshot, a report
// that never arrived, a malformed one — none of which may hide the HUD, in any
// mode.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  HUD_VISIBILITY_MODES,
  DEFAULT_HUD_VISIBILITY,
  DEFAULT_HUD_NOTIFICATIONS,
  readHudSettings,
  mergeHudSettings,
  summarizeFeedReport,
  awaitingSignature,
  acknowledgeAttention,
  carryAcknowledged,
  decideHud,
} = require("../src/hudVisibility");

/** A fully-resolved report: readable, exact, fresh. */
function report(overrides = {}) {
  return {
    readable: true,
    exact: true,
    stale: false,
    active: 0,
    awaiting: 0,
    unresolved: 0,
    awaitingIds: [],
    ...overrides,
  };
}

/** A report with `n` named sessions blocked on a human. */
function waitingOn(...ids) {
  return report({ awaiting: ids.length, awaitingIds: ids });
}

/** What collapsing the panel on those sessions records. */
function dismissalOf(...ids) {
  return acknowledgeAttention(waitingOn(...ids));
}

/** The notification preferences a file that never mentioned them reads as. */
function defaultNotifications(overrides = {}) {
  return { ...DEFAULT_HUD_NOTIFICATIONS, ...overrides };
}

/** Every field of a read that could not be made — unknown, never off. */
const unknown = {
  readable: false,
  enabled: null,
  mode: null,
  notifications: null,
  sound: null,
};

describe("readHudSettings", () => {
  it("reads a never-configured install as off, on the default mode", () => {
    assert.deepEqual(readHudSettings({}), {
      readable: true,
      enabled: false,
      mode: DEFAULT_HUD_VISIBILITY,
      notifications: defaultNotifications(),
      sound: false,
    });
  });

  it("reads a stored blob back", () => {
    assert.deepEqual(readHudSettings({ hud: { enabled: true, mode: "attention-only" } }), {
      readable: true,
      enabled: true,
      mode: "attention-only",
      notifications: defaultNotifications(),
      sound: false,
    });
  });

  it("defaults the mode when only `enabled` was ever written", () => {
    assert.deepEqual(readHudSettings({ hud: { enabled: true } }), {
      readable: true,
      enabled: true,
      mode: DEFAULT_HUD_VISIBILITY,
      notifications: defaultNotifications(),
      sound: false,
    });
  });

  it("reports an unreadable settings file as unknown, NOT as off", () => {
    // null is what main.js passes when settings.json could not be read.
    assert.deepEqual(readHudSettings(null), unknown);
    assert.deepEqual(readHudSettings("nonsense"), unknown);
    assert.deepEqual(readHudSettings([]), unknown);
  });

  it("reports a malformed hud blob as unknown, NOT as off", () => {
    // Hand-edited files: a non-object blob, a missing/non-boolean `enabled`,
    // and a mode this build doesn't know are all uninterpretable.
    assert.deepEqual(readHudSettings({ hud: null }), unknown);
    assert.deepEqual(readHudSettings({ hud: "on" }), unknown);
    assert.deepEqual(readHudSettings({ hud: {} }), unknown);
    assert.deepEqual(readHudSettings({ hud: { enabled: "yes" } }), unknown);
    assert.deepEqual(readHudSettings({ hud: { enabled: true, mode: "whenever" } }), unknown);
  });

  it("offers exactly the three documented modes", () => {
    assert.deepEqual(HUD_VISIBILITY_MODES, ["always", "hide-when-idle", "attention-only"]);
    assert.ok(HUD_VISIBILITY_MODES.includes(DEFAULT_HUD_VISIBILITY));
  });
});

describe("readHudSettings — notification preferences", () => {
  // The compatibility case, and the reason the rule is "absent = default"
  // rather than "absent = unreadable": this file was written by a build that
  // had no notification settings at all, and it is perfectly interpretable.
  it("reads a blob written by an older build as the DEFAULTS, not as unknown", () => {
    const read = readHudSettings({ hud: { enabled: true, mode: "hide-when-idle" } });
    assert.equal(read.readable, true);
    assert.deepEqual(read.notifications, defaultNotifications());
  });

  it("reads stored preferences back, filling only what was never written", () => {
    const read = readHudSettings({
      hud: {
        enabled: true,
        notifications: { budget: false, quietFrom: "22:00", quietTo: "07:00", budgetThreshold: 0.5 },
      },
    });
    assert.deepEqual(
      read.notifications,
      defaultNotifications({
        budget: false,
        quietFrom: "22:00",
        quietTo: "07:00",
        budgetThreshold: 0.5,
      }),
    );
  });

  it("reports a malformed preference as unknown, NOT as off", () => {
    const hud = (notifications) => ({ hud: { enabled: true, notifications } });
    assert.deepEqual(readHudSettings(hud(null)), unknown);
    assert.deepEqual(readHudSettings(hud("all")), unknown);
    assert.deepEqual(readHudSettings(hud([])), unknown);
    assert.deepEqual(readHudSettings(hud({ permission: "yes" })), unknown);
  });

  it("ignores a key it doesn't know, rather than calling the file unreadable", () => {
    // A key from a NEWER build is not a value we misread — every field this
    // build knows is still there and still right. Refusing to read the file
    // would break a downgrade for no gain. (Writing one is another matter:
    // hudIpc rejects unknown keys so this build never produces them.)
    const read = readHudSettings({ hud: { enabled: true, notifications: { fromTheFuture: 1 } } });
    assert.equal(read.readable, true);
    assert.deepEqual(read.notifications, defaultNotifications());
  });

  it("refuses half a quiet range, and any time that isn't HH:MM", () => {
    const hud = (notifications) => ({ hud: { enabled: true, notifications } });
    assert.deepEqual(readHudSettings(hud({ quietFrom: "22:00" })), unknown);
    assert.deepEqual(readHudSettings(hud({ quietTo: "07:00" })), unknown);
    assert.deepEqual(readHudSettings(hud({ quietFrom: "9:5", quietTo: "07:00" })), unknown);
    assert.deepEqual(readHudSettings(hud({ quietFrom: "24:00", quietTo: "07:00" })), unknown);
    // Explicit nulls are how the UI clears the range: no quiet hours, readable.
    const cleared = readHudSettings(hud({ quietFrom: null, quietTo: null }));
    assert.equal(cleared.readable, true);
    assert.equal(cleared.notifications.quietFrom, null);
  });

  it("refuses a threshold that would fire on everything or on nothing", () => {
    const hud = (budgetThreshold) => ({ hud: { enabled: true, notifications: { budgetThreshold } } });
    // 0 would warn about every session that declares a limit, the moment it
    // does; above 1 can never be reached. Neither may be read as a setting.
    assert.deepEqual(readHudSettings(hud(0)), unknown);
    assert.deepEqual(readHudSettings(hud(-0.5)), unknown);
    assert.deepEqual(readHudSettings(hud(1.5)), unknown);
    assert.deepEqual(readHudSettings(hud("80%")), unknown);
    assert.deepEqual(readHudSettings(hud(Number.NaN)), unknown);
    assert.equal(readHudSettings(hud(1)).notifications.budgetThreshold, 1);
  });

  it("surfaces the app-wide sound preference, and unknown when it can't be read", () => {
    assert.equal(readHudSettings({ notification_sound_enabled: true }).sound, true);
    // Opt-in: never written means silent, which is a fact.
    assert.equal(readHudSettings({}).sound, false);
    assert.deepEqual(readHudSettings({ notification_sound_enabled: "on" }), unknown);
  });
});

describe("mergeHudSettings", () => {
  it("keeps every other preference in the file", () => {
    const read = { ok: true, settings: { server_url: "https://a", recent_servers: ["https://a"] } };
    const { settings } = mergeHudSettings(read, { enabled: true });
    assert.deepEqual(settings, {
      server_url: "https://a",
      recent_servers: ["https://a"],
      hud: {
        enabled: true,
        mode: DEFAULT_HUD_VISIBILITY,
        notifications: defaultNotifications(),
      },
    });
  });

  it("REFUSES to write over a settings.json it could not read", () => {
    // The read failed, so the file's contents are unknown — saving here would
    // replace the saved server, the recents and everything else with a file
    // holding one hud blob. Refusing keeps the file for the user to fix.
    assert.throws(() => mergeHudSettings({ ok: false, settings: null }, { enabled: false }));
    assert.throws(() => mergeHudSettings(null, { enabled: false }));
  });

  it("writes on first launch, when the file is merely absent", () => {
    const { settings, hud } = mergeHudSettings({ ok: true, settings: {} }, { enabled: true });
    assert.deepEqual(hud, {
      enabled: true,
      mode: DEFAULT_HUD_VISIBILITY,
      notifications: defaultNotifications(),
    });
    assert.deepEqual(settings.hud, hud);
  });

  it("replaces a malformed hud blob inside a readable file", () => {
    const read = { ok: true, settings: { server_url: "https://a", hud: "on" } };
    const { settings } = mergeHudSettings(read, { mode: "attention-only" });
    assert.deepEqual(settings.hud, {
      enabled: false,
      mode: "attention-only",
      notifications: defaultNotifications(),
    });
    assert.equal(settings.server_url, "https://a", "the rest of the file survives");
  });

  // The whole reason the merge is two levels deep. The UI patches ONE switch
  // at a time, and a shallow spread would replace the sub-object — turning
  // "silence completions" into "silence everything except completions".
  it("a partial notifications patch leaves the other preferences alone", () => {
    const read = {
      ok: true,
      settings: {
        hud: {
          enabled: true,
          notifications: {
            permission: true,
            budget: false,
            stuck: true,
            completion: true,
            quietFrom: "22:00",
            quietTo: "07:00",
            budgetThreshold: 0.5,
          },
        },
      },
    };
    const { hud } = mergeHudSettings(read, { notifications: { completion: false } });
    assert.deepEqual(hud.notifications, {
      permission: true,
      budget: false,
      stuck: true,
      completion: false,
      quietFrom: "22:00",
      quietTo: "07:00",
      budgetThreshold: 0.5,
    });
  });

  it("keeps the visibility settings when only a notification changes", () => {
    const read = { ok: true, settings: { hud: { enabled: true, mode: "attention-only" } } };
    const { hud } = mergeHudSettings(read, { notifications: { budgetThreshold: 0.9 } });
    assert.equal(hud.enabled, true);
    assert.equal(hud.mode, "attention-only");
    assert.equal(hud.notifications.budgetThreshold, 0.9);
  });

  it("writes the sound to the APP-WIDE key, not into the hud blob", () => {
    // One sound preference, shared with the native Notifications menu. A copy
    // under `hud` would be a second switch that could disagree with it.
    const read = { ok: true, settings: { hud: { enabled: true } } };
    const { settings, sound } = mergeHudSettings(read, { sound: true });
    assert.equal(settings.notification_sound_enabled, true);
    assert.equal(sound, true);
    assert.ok(!("sound" in settings.hud));
  });

  it("leaves the sound untouched when the patch doesn't mention it", () => {
    const read = { ok: true, settings: { notification_sound_enabled: true } };
    const { settings } = mergeHudSettings(read, { enabled: true });
    assert.equal(settings.notification_sound_enabled, true);
  });
});

describe("summarizeFeedReport", () => {
  it("calls a fresh, exact, all-zero feed idle", () => {
    assert.deepEqual(summarizeFeedReport(report()), { attention: false, idleCertain: true });
  });

  it("never calls an unresolved feed idle", () => {
    for (const overrides of [
      { readable: false },
      { exact: false }, // counts are a FLOOR, so zero means "at least zero"
      { stale: true }, // the numbers stopped refreshing
      { unresolved: 2 }, // sessions the feed could not resolve or had to omit
      { active: 1 },
    ]) {
      assert.equal(
        summarizeFeedReport(report(overrides)).idleCertain,
        false,
        `${JSON.stringify(overrides)} must not read as idle`,
      );
    }
  });

  it("never calls a missing or malformed report idle", () => {
    for (const value of [null, undefined, "idle", {}, report({ awaiting: "0" })]) {
      assert.equal(summarizeFeedReport(value).idleCertain, false);
    }
  });

  it("flags attention on any positive awaiting count, floor or stale included", () => {
    assert.equal(summarizeFeedReport(report({ awaiting: 1 })).attention, true);
    assert.equal(summarizeFeedReport(report({ awaiting: 3, exact: false })).attention, true);
    assert.equal(summarizeFeedReport(report({ awaiting: 1, stale: true })).attention, true);
    assert.equal(summarizeFeedReport(report({ awaiting: 0 })).attention, false);
  });
});

describe("decideHud", () => {
  it("keeps the HUD closed while the setting is off or unknown", () => {
    for (const enabled of [false, null, undefined]) {
      assert.deepEqual(decideHud({ enabled, mode: "always", report: report({ awaiting: 5 }) }), {
        visible: false,
        expanded: false,
        autoExpanded: false,
      });
    }
  });

  it("always mode keeps a fully idle HUD on screen", () => {
    const decision = decideHud({ enabled: true, mode: "always", report: report() });
    assert.equal(decision.visible, true);
  });

  it("hide-when-idle hides only a PROVEN idle feed", () => {
    assert.equal(
      decideHud({ enabled: true, mode: "hide-when-idle", report: report() }).visible,
      false,
    );
    assert.equal(
      decideHud({ enabled: true, mode: "hide-when-idle", report: report({ active: 1 }) }).visible,
      true,
    );
  });

  it("hide-when-idle keeps a degraded feed visible — unreadable is not idle", () => {
    // The whole point: an all-zero count we could not resolve must not be
    // mistaken for silence and hide the monitor.
    for (const overrides of [
      { readable: false },
      { exact: false },
      { stale: true },
      { unresolved: 1 },
    ]) {
      assert.equal(
        decideHud({ enabled: true, mode: "hide-when-idle", report: report(overrides) }).visible,
        true,
        `${JSON.stringify(overrides)} must keep the HUD visible`,
      );
    }
    // Same for a report that never arrived, or arrived malformed.
    assert.equal(decideHud({ enabled: true, mode: "hide-when-idle", report: null }).visible, true);
    assert.equal(decideHud({ enabled: true, mode: "hide-when-idle", report: {} }).visible, true);
  });

  it("attention-only hides settled work but never an unresolved feed", () => {
    // Running, nothing waiting on a human, and the feed says so exactly.
    assert.equal(
      decideHud({ enabled: true, mode: "attention-only", report: report({ active: 3 }) }).visible,
      false,
    );
    assert.equal(
      decideHud({ enabled: true, mode: "attention-only", report: report({ exact: false }) })
        .visible,
      true,
    );
    assert.equal(decideHud({ enabled: true, mode: "attention-only", report: null }).visible, true);
  });

  it("does not claim an expansion the user already had", () => {
    // Attention arriving on a HUD the user opened by hand has nothing to
    // expand — and must not mark it as the shell's to collapse later.
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: report({ awaiting: 1 }),
      expanded: true,
      autoExpanded: false,
    });
    assert.deepEqual(decision, { visible: true, expanded: null, autoExpanded: false });
  });

  it("keeps owning an expansion it already caused when attention persists", () => {
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: report({ awaiting: 2 }),
      expanded: true,
      autoExpanded: true,
    });
    assert.deepEqual(decision, { visible: true, expanded: null, autoExpanded: true });
  });

  it("shows and expands on attention, in every mode", () => {
    for (const mode of HUD_VISIBILITY_MODES) {
      assert.deepEqual(
        decideHud({ enabled: true, mode, report: report({ active: 1, awaiting: 1 }) }),
        { visible: true, expanded: true, autoExpanded: true },
        `mode ${mode} must surface attention`,
      );
    }
  });

  it("falls back to the default mode when the stored mode is unknown", () => {
    // An unrecognized mode must not become "hide everything".
    assert.equal(decideHud({ enabled: true, mode: null, report: report() }).visible, true);
    assert.equal(decideHud({ enabled: true, mode: "whenever", report: report() }).visible, true);
  });

  it("collapses its OWN expansion once attention clears", () => {
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: report({ active: 1 }),
      autoExpanded: true,
    });
    assert.deepEqual(decision, { visible: true, expanded: false, autoExpanded: false });
  });

  it("leaves a manual expansion alone when attention clears", () => {
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: report({ active: 1 }),
      autoExpanded: false,
    });
    assert.equal(decision.expanded, null, "no auto-expand to undo → no opinion");
  });

  it("does not re-expand for attention the user already dismissed", () => {
    // The persistent-permission case: one prompt sits there for minutes, and
    // every poll used to re-open the panel the user had just closed.
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: waitingOn("s1"),
      expanded: false,
      acknowledged: dismissalOf("s1"),
    });
    assert.deepEqual(decision, { visible: true, expanded: null, autoExpanded: false });
  });

  it("expands for a session the user has NOT seen waiting yet", () => {
    const decision = decideHud({
      enabled: true,
      mode: "always",
      report: waitingOn("s1", "s2"),
      expanded: false,
      acknowledged: dismissalOf("s1"),
    });
    assert.deepEqual(decision, { visible: true, expanded: true, autoExpanded: true });
  });

  it("re-expands when the same session blocks again after really clearing", () => {
    // "s1" left the waiting list on a report we could READ, so its return is
    // new attention, not the one already dismissed.
    const cleared = carryAcknowledged(dismissalOf("s1"), report());
    assert.deepEqual(cleared, { named: true, ids: [] });
    assert.equal(
      decideHud({
        enabled: true,
        mode: "always",
        report: waitingOn("s1"),
        acknowledged: cleared,
      }).expanded,
      true,
    );
  });

  it("never re-expands on attention it cannot name", () => {
    // The mirror of the visibility rule: not-knowing keeps the HUD on screen,
    // but it may not re-open a panel the user closed — an unreadable list
    // cannot tell a new prompt from the one already dismissed.
    for (const overrides of [
      { exact: false },
      { stale: true },
      { readable: false },
      { unresolved: 1 },
      { awaitingIds: undefined },
      { awaitingIds: "s1" },
      { awaitingIds: [1] },
      { awaitingIds: ["s1", "s1"] }, // two entries, one identity
      { awaitingIds: [] }, // names none of the sessions it counts
      { awaitingIds: ["s1", "s2"] }, // names more than it counts
    ]) {
      const decision = decideHud({
        enabled: true,
        mode: "always",
        report: report({ awaiting: 1, awaitingIds: ["s1"], ...overrides }),
        expanded: false,
        acknowledged: dismissalOf("s9"),
      });
      assert.deepEqual(
        decision,
        { visible: true, expanded: null, autoExpanded: false },
        `${JSON.stringify(overrides)} must stay visible without re-expanding`,
      );
    }
  });

  it("expands on attention while the user has dismissed nothing", () => {
    // No acknowledgement at all: an unnameable report still auto-expands, which
    // is the pre-existing behaviour and the reason uncertainty is not enough to
    // SUPPRESS the first expansion either.
    assert.equal(
      decideHud({ enabled: true, mode: "always", report: report({ awaiting: 1 }) }).expanded,
      true,
    );
  });

  it("keeps an unnameable dismissal until the feed can name what is waiting", () => {
    // The user closed the panel while the feed was stale. Nothing is proven
    // gone, so the dismissal stands…
    const dismissed = acknowledgeAttention(report({ awaiting: 1, stale: true }));
    assert.equal(
      decideHud({
        enabled: true,
        mode: "always",
        report: report({ awaiting: 1, stale: true }),
        acknowledged: dismissed,
      }).expanded,
      null,
    );
    // …and once the feed recovers, it is the list it can now name that was
    // dismissed — not an all-clear that re-opens the panel.
    const named = carryAcknowledged(dismissed, waitingOn("s1"));
    assert.deepEqual(named, { named: true, ids: ["s1"] });
    assert.equal(
      decideHud({ enabled: true, mode: "always", report: waitingOn("s1"), acknowledged: named })
        .expanded,
      null,
    );
  });

  it("cannot be deafened by a session named like an internal marker", () => {
    // A dismissal the feed could not name is its own STATE, not a reserved id.
    // Held as a magic string, a session actually called that would read as
    // "the user dismissed something unnameable" — and the next real prompt
    // would be swallowed as already-seen.
    for (const impostor of ["<unnamed attention>", "named", "false", "[object Object]"]) {
      const dismissed = dismissalOf(impostor);
      const carried = carryAcknowledged(dismissed, waitingOn(impostor, "s_new"));
      assert.deepEqual(
        decideHud({
          enabled: true,
          mode: "always",
          report: waitingOn(impostor, "s_new"),
          acknowledged: carried,
        }),
        { visible: true, expanded: true, autoExpanded: true },
        `a session called "${impostor}" hid a new prompt`,
      );
    }
  });

  it("names the waiting sessions only from a report that proves the list", () => {
    assert.deepEqual(awaitingSignature(waitingOn("s1", "s2")), ["s1", "s2"]);
    assert.deepEqual(awaitingSignature(report()), []);
    assert.equal(awaitingSignature(null), null);
    assert.equal(
      awaitingSignature(report({ awaiting: 1, awaitingIds: ["s1"], exact: false })),
      null,
    );
  });

  it("does not collapse an auto-expansion while attention is merely unknown", () => {
    // The feed stopped resolving after the HUD auto-expanded: nothing here
    // proves the blocked session got answered.
    const decision = decideHud({
      enabled: true,
      mode: "hide-when-idle",
      report: report({ readable: false, awaiting: 0 }),
      autoExpanded: true,
    });
    assert.deepEqual(decision, { visible: true, expanded: null, autoExpanded: true });
  });
});
