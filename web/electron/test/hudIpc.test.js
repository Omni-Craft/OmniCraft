// Behavior tests for the HUD's IPC surface (src/hudIpc.js), run with
// `node --test`. A fake ipcMain captures the handlers and the tests INVOKE
// them — with the live HUD as sender, with a foreign sender, with junk
// payloads — and assert what reached the policy.
//
// This is the half a source-text assertion cannot see: that the message is
// actually delivered, and that everything else is actually dropped. These
// messages move an always-on-top window, so "dropped" is a security property.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { registerHudIpc } = require("../src/hudIpc");

/** Fake ipcMain plus a policy spy; returns the seams a test needs to drive. */
function harness({
  pinned = true,
  settings = { readable: true, enabled: true, mode: "always" },
} = {}) {
  const listeners = new Map();
  const handlers = new Map();
  const calls = [];
  const hudWebContents = { id: "hud" };
  const state = { settings: { ...settings }, writeFails: false, warnings: [], hudOpen: true };

  registerHudIpc({
    ipcMain: {
      on: (channel, fn) => listeners.set(channel, fn),
      handle: (channel, fn) => handlers.set(channel, fn),
    },
    policy: {
      setUserExpanded: (v) => calls.push(["setUserExpanded", v]),
      setFeedReport: (r) => calls.push(["setFeedReport", r]),
      applyPolicy: () => calls.push(["applyPolicy"]),
    },
    getHudWebContents: () => (state.hudOpen ? hudWebContents : null),
    isPinnedOriginSender: () => pinned,
    readSettings: () => ({ ...state.settings }),
    writeSettings: (patch) => {
      if (state.writeFails) throw new Error("settings.json could not be read");
      Object.assign(state.settings, patch);
      calls.push(["writeSettings", patch]);
    },
    onWarn: (message) => state.warnings.push(message),
  });

  return {
    state,
    calls,
    fromHud: { sender: hudWebContents },
    fromElsewhere: { sender: { id: "other" } },
    send: (channel, event, ...args) => listeners.get(channel)(event, ...args),
    // async so a handler that throws synchronously still surfaces as a
    // rejection — which is what `invoke` gives the renderer.
    invoke: async (channel, event, ...args) => handlers.get(channel)(event, ...args),
  };
}

describe("hudIpc — only the live HUD may drive its window", () => {
  it("delivers a feed report from the HUD to the policy", () => {
    const h = harness();
    const report = {
      readable: true,
      exact: true,
      stale: false,
      active: 1,
      awaiting: 1,
      unresolved: 0,
    };
    h.send("omnicraft:hud-report-feed", h.fromHud, report);
    assert.deepEqual(h.calls, [["setFeedReport", report]]);
  });

  it("drops a feed report from any other renderer", () => {
    const h = harness();
    h.send("omnicraft:hud-report-feed", h.fromElsewhere, { readable: true });
    assert.deepEqual(h.calls, []);
    assert.equal(h.state.warnings.length, 1);
  });

  it("drops HUD messages once the HUD is gone", () => {
    const h = harness();
    h.state.hudOpen = false;
    h.send("omnicraft:hud-report-feed", h.fromHud, { readable: true });
    h.send("omnicraft:hud-set-expanded", h.fromHud, true);
    assert.deepEqual(h.calls, []);
  });

  it("normalizes a missing report to null rather than passing undefined through", () => {
    const h = harness();
    h.send("omnicraft:hud-report-feed", h.fromHud);
    assert.deepEqual(h.calls, [["setFeedReport", null]]);
  });

  it("passes an expand request through as the USER's, coerced to a boolean", () => {
    const h = harness();
    h.send("omnicraft:hud-set-expanded", h.fromHud, "yes");
    h.send("omnicraft:hud-set-expanded", h.fromHud, true);
    assert.deepEqual(h.calls, [
      ["setUserExpanded", false],
      ["setUserExpanded", true],
    ]);
  });
});

describe("hudIpc — the Settings section's calls", () => {
  it("returns the settings, readable flag included", async () => {
    const h = harness({ settings: { readable: false, enabled: null, mode: null } });
    assert.deepEqual(await h.invoke("omnicraft:hud-get-settings", h.fromHud), {
      readable: false,
      enabled: null,
      mode: null,
    });
  });

  it("gives an unpinned page nothing", async () => {
    const h = harness({ pinned: false });
    assert.equal(await h.invoke("omnicraft:hud-get-settings", h.fromElsewhere), null);
    await assert.rejects(() => h.invoke("omnicraft:hud-set-settings", h.fromElsewhere, {}));
  });

  it("persists a mode change and re-applies the policy", async () => {
    const h = harness();
    await h.invoke("omnicraft:hud-set-settings", h.fromHud, { mode: "attention-only" });
    assert.deepEqual(h.calls, [["writeSettings", { mode: "attention-only" }], ["applyPolicy"]]);
    assert.equal(h.state.settings.mode, "attention-only");
  });

  it("refuses values it does not document, without touching the file", async () => {
    const h = harness();
    await assert.rejects(() =>
      h.invoke("omnicraft:hud-set-settings", h.fromHud, { mode: "whenever" }),
    );
    await assert.rejects(() =>
      h.invoke("omnicraft:hud-set-settings", h.fromHud, { enabled: "on" }),
    );
    assert.deepEqual(h.calls, []);
  });

  it("propagates an unwritable settings file instead of reporting success", async () => {
    // The renderer turns a rejection into "não pôde ser salva"; swallowing it
    // here would leave Settings showing a value that was never persisted.
    const h = harness();
    h.state.writeFails = true;
    await assert.rejects(() =>
      h.invoke("omnicraft:hud-set-settings", h.fromHud, { enabled: false }),
    );
    assert.deepEqual(h.calls, [], "the policy must not run on a write that failed");
  });
});
