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
    ...overrides,
  };
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
    assert.equal(state.visible, true);
  });

  it("turns the HUD OFF when it is on screen, and persists that", () => {
    const { state, policy } = harness();
    state.arrive();
    assert.equal(state.visible, true);

    policy.toggle();
    assert.equal(state.settings.enabled, false);
    assert.ok(state.calls.includes("close"));
  });

  it("hands control back to the mode once the feed shows the HUD by itself", () => {
    const { state, policy } = harness({
      settings: { readable: true, enabled: true, mode: "attention-only" },
    });
    state.arrive();
    policy.setFeedReport(report({ awaiting: 0 }));
    policy.toggle(); // manual reveal
    assert.equal(state.visible, true);

    policy.setFeedReport(report({ awaiting: 1 })); // the feed shows it on its own
    policy.setFeedReport(report({ awaiting: 0 })); // …and it settles again
    assert.equal(state.visible, false, "the reveal outlived the mode");
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
      revealed: false,
    });
  });
});
