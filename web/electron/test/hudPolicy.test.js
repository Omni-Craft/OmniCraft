// Behavior tests for the floating HUD's state machine (src/hudPolicy.js), run
// with `node --test`. A fake window records what the shell asked it to do, so
// these exercise the actual sequence of calls — not the presence of a function
// name in main.js.
//
// The invariant under the microscope: **the shell may only undo what the shell
// did.** A HUD the user expanded by hand stays expanded, including across the
// arrival and departure of attention that happened alongside it.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { createHudPolicy } = require("../src/hudPolicy");

/** A feed report: resolved, fresh, exact. */
function report(overrides = {}) {
  return {
    readable: true,
    exact: true,
    stale: false,
    active: 1,
    awaiting: 0,
    unresolved: 0,
    awaitingIds: [],
    ...overrides,
  };
}

/** A report naming the sessions blocked on a human. */
function waitingOn(...ids) {
  return report({ active: ids.length, awaiting: ids.length, awaitingIds: ids });
}

/**
 * A stand-in for the HUD window plus the settings file, wired into a policy.
 * `calls` keeps the ordered log so a test can assert what the shell did, not
 * just where it ended up.
 */
function harness({
  settings = { readable: true, enabled: true, mode: "always" },
  ready = true,
} = {}) {
  const state = {
    settings: { ...settings },
    ready,
    visible: false,
    exists: false,
    expanded: false,
    notified: [],
    calls: [],
    writeFails: false,
    /** Reports handed to the desktop-notification watcher, in order. */
    observed: [],
  };

  const win = {
    isReady: () => state.ready,
    isVisible: () => state.visible,
    show: () => {
      state.visible = true;
      state.calls.push("show");
    },
    hide: () => {
      state.visible = false;
      state.calls.push("hide");
    },
    close: () => {
      state.exists = false;
      state.visible = false;
      state.calls.push("close");
    },
    setExpanded: (expanded, notifyRenderer) => {
      state.expanded = expanded;
      state.calls.push(`expanded:${expanded}`);
      if (notifyRenderer) state.notified.push(expanded);
    },
  };

  const policy = createHudPolicy({
    readSettings: () => state.settings,
    writeSettings: (patch) => {
      if (state.writeFails) throw new Error("settings.json could not be read");
      Object.assign(state.settings, patch);
      state.calls.push(`write:${JSON.stringify(patch)}`);
    },
    getWindow: () => (state.exists ? win : null),
    openWindow: () => {
      state.exists = true;
      state.calls.push("open");
    },
    notifier: {
      observe: (report) => {
        state.observed.push(report);
        state.calls.push("observe");
      },
      reset: () => {
        state.observed.push("reset");
        state.calls.push("notifier:reset");
      },
    },
  });

  /** Pretend the window opened and painted (what main.js does on ready-to-show). */
  state.arrive = () => {
    state.exists = true;
    state.ready = true;
    policy.windowReady();
  };

  return { state, policy };
}

describe("hudPolicy — the shell only undoes what the shell did", () => {
  it("keeps a MANUAL expansion after attention arrives and then clears", () => {
    // The full sequence the contract names: the user opens the list, a session
    // blocks, the session is answered. The list is still open — the user never
    // asked for it to close.
    const { state, policy } = harness();
    state.exists = true;
    state.visible = true;

    policy.setUserExpanded(true);
    assert.equal(state.expanded, true);

    policy.setFeedReport(report({ awaiting: 1 }));
    assert.equal(state.expanded, true, "already expanded — nothing to expand");

    policy.setFeedReport(report({ awaiting: 0 }));
    assert.equal(state.expanded, true, "the shell collapsed an expansion it did not cause");
    assert.equal(policy.state().autoExpanded, false);
  });

  it("collapses its OWN expansion after attention clears", () => {
    // The mirror: nobody touched the panel, so the expansion is the shell's and
    // the shell puts it back.
    const { state, policy } = harness();
    state.exists = true;
    state.visible = true;

    policy.setFeedReport(report({ awaiting: 1 }));
    assert.equal(state.expanded, true);
    assert.deepEqual(state.notified, [true], "the panel is told the shell expanded it");

    policy.setFeedReport(report({ awaiting: 0 }));
    assert.equal(state.expanded, false);
    assert.deepEqual(state.notified, [true, false]);
  });

  it("does not collapse while the feed can no longer say attention cleared", () => {
    const { state, policy } = harness();
    state.exists = true;
    state.visible = true;

    policy.setFeedReport(report({ awaiting: 1 }));
    policy.setFeedReport(report({ readable: false, awaiting: 0 }));
    assert.equal(state.expanded, true);
  });
});

describe("hudPolicy — attention the user has already seen", () => {
  it("does not re-open the panel while the SAME permission stays pending", () => {
    // A pending approval sits there across polls. Collapsing the panel says "I
    // have seen this"; re-expanding it three seconds later takes that back.
    const { state, policy } = harness();
    state.arrive();

    policy.setFeedReport(waitingOn("s1"));
    assert.equal(state.expanded, true, "new attention expands");

    policy.setUserExpanded(false);
    assert.equal(state.expanded, false);

    policy.setFeedReport(waitingOn("s1"));
    policy.setFeedReport(waitingOn("s1"));
    assert.equal(state.expanded, false, "the shell re-opened what the user closed");
    assert.equal(state.visible, true, "still on screen — something IS waiting");
  });

  it("re-opens for a session that starts waiting after the user collapsed", () => {
    const { state, policy } = harness();
    state.arrive();

    policy.setFeedReport(waitingOn("s1"));
    policy.setUserExpanded(false);
    policy.setFeedReport(waitingOn("s1"));
    assert.equal(state.expanded, false);

    policy.setFeedReport(waitingOn("s1", "s2"));
    assert.equal(state.expanded, true, "a session the user never saw waiting must surface");
  });

  it("re-opens when the same session blocks again after clearing", () => {
    const { state, policy } = harness();
    state.arrive();

    policy.setFeedReport(waitingOn("s1"));
    policy.setUserExpanded(false);
    policy.setFeedReport(report({ awaiting: 0 })); // answered — proven gone
    policy.setFeedReport(waitingOn("s1"));
    assert.equal(state.expanded, true);
  });

  it("does not re-open on attention it cannot name", () => {
    // A floor, a stale snapshot or a list that accounts for nothing cannot tell
    // a new prompt from the one the user dismissed. Uncertainty keeps the HUD
    // visible; it does not re-open it.
    for (const overrides of [
      { exact: false },
      { stale: true },
      { awaitingIds: undefined },
      { awaitingIds: [] },
    ]) {
      const { state, policy } = harness();
      state.arrive();
      policy.setFeedReport(waitingOn("s1"));
      policy.setUserExpanded(false);

      policy.setFeedReport(report({ awaiting: 1, awaitingIds: ["s1"], ...overrides }));
      assert.equal(
        state.expanded,
        false,
        `${JSON.stringify(overrides)} re-opened a panel the user closed`,
      );
      assert.equal(state.visible, true);
    }
  });

  it("expands on attention the user never dismissed, named or not", () => {
    const { state, policy } = harness();
    state.arrive();

    policy.setFeedReport(report({ awaiting: 1, exact: false }));
    assert.equal(state.expanded, true, "an uncertain feed still surfaces attention");
  });
});

describe("hudPolicy — showing and hiding", () => {
  it("never shows a window that has not painted (no startup flicker)", () => {
    const { state, policy } = harness({ ready: false });
    state.exists = true;

    policy.applyPolicy();
    assert.equal(state.visible, false);
    assert.ok(!state.calls.includes("show"), "shown before ready-to-show");

    // ready-to-show → now the policy may show it.
    state.arrive();
    assert.equal(state.visible, true);
  });

  it("hides an idle HUD under hide-when-idle, and brings it back on attention", () => {
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "hide-when-idle" },
    });
    state.arrive();
    assert.equal(state.visible, true);

    policy.setFeedReport(report({ active: 0 }));
    assert.equal(state.visible, false, "a proven-idle feed hides it");

    policy.setFeedReport(report({ active: 1, awaiting: 1 }));
    assert.equal(state.visible, true);
  });

  it("keeps a degraded feed on screen — unreadable is not idle", () => {
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "hide-when-idle" },
    });
    state.arrive();

    policy.setFeedReport(report({ readable: false, active: 0 }));
    assert.equal(state.visible, true);
  });

  it("opens the saved HUD when the policy runs with no window (startup)", () => {
    const { state, policy } = harness();
    state.exists = false;

    policy.applyPolicy();
    assert.deepEqual(state.calls, ["open"]);
  });

  it("does not open anything while the settings are unknown", () => {
    const { state, policy } = harness({
      settings: { readable: false, enabled: null, mode: null },
    });

    policy.applyPolicy();
    assert.deepEqual(state.calls, [], "an unread setting must not open an always-on-top window");
  });

  it("closes the window when the setting is off", () => {
    const { state, policy } = harness();
    state.arrive();
    state.settings.enabled = false;

    policy.applyPolicy();
    assert.ok(state.calls.includes("close"));
  });
});

describe("hudPolicy — the menu toggle", () => {
  it("turns the HUD ON when the window exists but is HIDDEN by the mode", () => {
    // The regression: keying on the window's EXISTENCE turned the HUD "off"
    // when the user was asking to see it.
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "attention-only" },
    });
    state.arrive();
    policy.setFeedReport(report({ active: 2, awaiting: 0 }));
    assert.equal(state.visible, false, "attention-only hid it");

    policy.toggle();
    assert.equal(state.settings.enabled, true, "asking to see it must not persist off");
    assert.equal(state.settings.mode, "always", "the mode that was hiding it must give way");
    assert.equal(state.visible, true);
  });

  it("keeps the HUD on screen after the next feed report", () => {
    // The regression this replaces: the reveal was an in-memory flag the first
    // report consumed, so the HUD appeared and vanished a poll later.
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "attention-only" },
    });
    state.arrive();
    policy.setFeedReport(report({ active: 2, awaiting: 0 }));
    assert.equal(state.visible, false);

    policy.toggle();
    assert.equal(state.visible, true);

    policy.setFeedReport(report({ active: 2, awaiting: 0 }));
    assert.equal(state.visible, true, "the HUD the user asked for vanished on the next poll");
    policy.setFeedReport(report({ active: 0, awaiting: 0 }));
    assert.equal(state.visible, true, "…and on an idle one");
    // And the choice is one the user can see and undo in Settings.
    assert.equal(state.settings.mode, "always");
  });

  it("turns a HUD that was OFF back on, and it STAYS on under a hiding mode", () => {
    // Turning it on while the stored mode hides idle machines used to put the
    // window up and let the very next report take it away again: the user asked
    // to see the HUD and watched it flash. The mode gives way instead — it is
    // still there in Settings for them to put back.
    const { state, policy } = harness({
      settings: { readable: true, enabled: false, mode: "hide-when-idle" },
    });

    policy.toggle();
    assert.equal(state.settings.enabled, true);
    assert.equal(state.visible, true);

    policy.setFeedReport(report({ active: 0, awaiting: 0 }));
    assert.equal(state.visible, true, "the HUD the user asked for vanished on the next poll");
    assert.equal(state.settings.mode, "always");
  });

  it("turns the HUD OFF when it is on screen, and persists that", () => {
    const { state, policy } = harness();
    state.arrive();
    assert.equal(state.visible, true);

    policy.toggle();
    assert.equal(state.settings.enabled, false);
    assert.ok(state.calls.includes("close"));
  });

  it("does not act on a toggle it could not persist", () => {
    const { state, policy } = harness();
    state.arrive();
    state.writeFails = true;

    policy.toggle();
    assert.equal(state.visible, true, "the HUD closed on a setting that was never saved");
  });
});

describe("hudPolicy — the window going away", () => {
  it("persists OFF when the user closes the HUD", () => {
    // Otherwise the next feed report (or the next launch) reopens a window the
    // user just dismissed.
    const { state, policy } = harness();
    state.arrive();

    policy.windowClosed({ userInitiated: true });
    assert.equal(state.settings.enabled, false);
  });

  it("does NOT persist off when the shell announced the close", () => {
    // Quitting the app, or the last shell window going away, closes the HUD.
    // That is not the user turning the feature off — without this, every Cmd-Q
    // would disable the HUD and it would never come back.
    const { state, policy } = harness();
    state.arrive();

    policy.shellClosing();
    policy.windowClosed();
    assert.equal(state.settings.enabled, true);
  });

  it("treats an unannounced close as the user's, with no flag left behind", () => {
    const { state, policy } = harness();
    state.arrive();

    policy.shellClosing();
    policy.windowClosed(); // the shell's close consumes the announcement…
    state.settings.enabled = true;
    state.arrive();
    policy.windowClosed(); // …so the NEXT close is the user's again
    assert.equal(state.settings.enabled, false);
  });

  it("does not persist off when disabling closes the window", () => {
    // Settings → off already wrote `enabled: false`; the close it causes must
    // not be mistaken for the user dismissing a HUD that was on.
    const { state, policy } = harness();
    state.arrive();
    state.settings.enabled = false;

    policy.applyPolicy();
    policy.windowClosed();
    assert.deepEqual(
      state.calls.filter((c) => typeof c === "string" && c.startsWith("write:")),
      [],
    );
  });

  it("survives a close it could not persist", () => {
    const { state, policy } = harness();
    state.arrive();
    state.writeFails = true;

    assert.doesNotThrow(() => policy.windowClosed({ userInitiated: true }));
  });

  it("forgets the closed window's feed report", () => {
    // The next HUD polls for itself; deciding a new window's visibility from a
    // snapshot nobody re-read is how a stale "idle" hides a busy machine.
    const { state, policy } = harness();
    state.arrive();
    policy.setFeedReport(report({ awaiting: 1 }));

    policy.shellClosing();
    policy.windowClosed();
    assert.deepEqual(policy.state(), {
      expanded: false,
      autoExpanded: false,
      report: null,
      acknowledged: null,
    });
  });
});

describe("hudPolicy — the toggle's choice is the settings, and nothing else", () => {
  it("lets Settings undo what the toggle did", () => {
    // The behaviour that says the toggle left NO state of its own behind: what
    // put the HUD on screen is the stored mode, so putting the mode back takes
    // it off again. A HUD held up by an in-memory reveal would sit there
    // through a mode the user just restored, with nothing to switch off.
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "attention-only" },
    });
    state.arrive();
    policy.setFeedReport(report({ active: 2, awaiting: 0 }));
    assert.equal(state.visible, false);

    policy.toggle();
    assert.equal(state.visible, true);

    // Settings → Desktop → HUD, back to "only on attention" (the SPA writes the
    // setting and re-runs the policy — see hudIpc).
    state.settings.mode = "attention-only";
    policy.applyPolicy();
    assert.equal(state.visible, false, "the HUD outlived the setting that was showing it");
  });
});

describe("hudPolicy — the desktop notifications ride on the feed report", () => {
  it("hands every report to the watcher, exactly as it arrived", () => {
    // Including the ones the shell itself will refuse to act on: the watcher
    // has its own, stricter, rules about what an uncertain report proves, and
    // it cannot apply them to a report it never sees.
    const { state, policy } = harness();
    state.arrive();

    const first = waitingOn("conv_a");
    const second = report({ readable: false });
    policy.setFeedReport(first);
    policy.setFeedReport(second);

    assert.deepEqual(state.observed, [first, second]);
  });

  it("passes an absent report through as null rather than swallowing it", () => {
    const { state, policy } = harness();
    state.arrive();

    policy.setFeedReport(undefined);
    assert.deepEqual(state.observed, [null]);
  });

  it("forgets the baseline when the window goes away", () => {
    // The next HUD seeds its own: a session that finished while nothing was
    // watching is not news the moment the HUD comes back.
    const { state, policy } = harness();
    state.arrive();
    policy.setFeedReport(report());

    policy.shellClosing();
    policy.windowClosed();
    assert.equal(state.observed.at(-1), "reset");
  });
});
