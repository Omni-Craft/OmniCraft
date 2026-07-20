// Tests for the HUD preload's edge validation (src/hud_preload.js), run with
// `node --test`. Electron is stubbed at the module loader, so the real preload
// runs and we read back exactly what it puts on the wire.
//
// This is the boundary the shell's notifications are decided from, so the rule
// it enforces is the interesting part: a field that doesn't match its shape
// travels as `null` — never coerced to `0` or `""` — and a row the preload
// cannot identify voids the WHOLE list rather than being quietly dropped. A
// silently shortened list is the dangerous one: the shell would read a session
// it never saw as one that had gone away.

const { describe, it, before } = require("node:test");
const assert = require("node:assert/strict");
const Module = require("node:module");

/** Messages the preload sent, in order. */
const sent = [];
/** The API object the preload exposed to the page. */
let api = null;

before(() => {
  const load = Module._load;
  Module._load = function stubbedLoad(request, parent, isMain) {
    if (request === "electron") {
      return {
        contextBridge: {
          exposeInMainWorld: (_name, value) => {
            api = value;
          },
        },
        ipcRenderer: {
          send: (channel, payload) => sent.push({ channel, payload }),
          on: () => {},
          removeListener: () => {},
        },
      };
    }
    return load.call(this, request, parent, isMain);
  };
  require("../src/hud_preload");
  Module._load = load;
});

/** Report the preload sent for `report`. */
function relay(report) {
  sent.length = 0;
  api.reportFeed(report);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].channel, "omnicraft:hud-report-feed");
  return sent[0].payload;
}

/** A well-formed row. */
function row(overrides = {}) {
  return {
    id: "conv_a",
    label: "Projeto A",
    status: "running",
    pending: 0,
    elicitationId: null,
    updatedAtMs: 1_700_000_000_000,
    costUsd: 1.5,
    maxCostUsd: 10,
    budgetUnreadable: false,
    ...overrides,
  };
}

describe("the HUD's feed report on the wire", () => {
  it("carries a well-formed row through unchanged", () => {
    const payload = relay({ readable: true, exact: true, sessions: [row()] });
    assert.deepEqual(payload.sessions, [row()]);
  });

  it("marks a truncated list as truncated", () => {
    assert.equal(relay({ truncated: true, sessions: [] }).truncated, true);
    assert.equal(relay({ truncated: false, sessions: [] }).truncated, false);
  });

  it("keeps a flag it could not read as unknown, not as false", () => {
    // The expensive one: `truncated: null` collapsed to `false` would tell the
    // shell the row list is COMPLETE — the exact proof it demands before it
    // prunes state and fires alerts — on the strength of a field nobody read.
    const payload = relay({ sessions: [] });
    assert.equal(payload.truncated, null);
    assert.equal(payload.readable, null);
    assert.equal(payload.exact, null);
    assert.equal(payload.stale, null);
    assert.equal(relay({ truncated: "no", sessions: [] }).truncated, null);
    assert.equal(relay({ stale: 0, sessions: [] }).stale, null);
  });

  it("keeps the observation-completeness claim three-valued", () => {
    // The shell prunes its "already notified" state on the strength of this
    // one: an unread claim arriving as `false` is harmless, arriving as
    // `true` would let a gap read as "that session is gone".
    assert.equal(relay({ observationComplete: true, sessions: [] }).observationComplete, true);
    assert.equal(relay({ observationComplete: false, sessions: [] }).observationComplete, false);
    assert.equal(relay({ observationComplete: "yes", sessions: [] }).observationComplete, null);
    assert.equal(relay({ sessions: [] }).observationComplete, null);
  });

  it("keeps a count it could not read as unknown, not as zero", () => {
    // `Number(null) === 0` is the trap: "I don't know how many sessions were
    // left out" would arrive as "none were", which is a proof of completeness.
    const payload = relay({ sessions: [] });
    assert.equal(payload.unresolved, null);
    assert.equal(payload.active, null);
    assert.equal(payload.awaiting, null);
    assert.equal(relay({ unresolved: null, sessions: [] }).unresolved, null);
  });

  it("does not accept a numeric string as a number", () => {
    // `Number("3")` would launder a payload we never agreed to into a count
    // the shell prunes and alerts on.
    const payload = relay({ active: "3", awaiting: "0", unresolved: "0", sessions: [] });
    assert.equal(payload.active, null);
    assert.equal(payload.awaiting, null);
    assert.equal(payload.unresolved, null);
  });

  it("carries the server's own clock, or nothing at all", () => {
    assert.equal(
      relay({ generatedAtMs: 1_700_000_000_000, sessions: [] }).generatedAtMs,
      1_700_000_000_000,
    );
    assert.equal(relay({ generatedAtMs: "1700000000000", sessions: [] }).generatedAtMs, null);
    assert.equal(relay({ sessions: [] }).generatedAtMs, null);
  });

  it("sends no rows at all when the page had none to send", () => {
    assert.equal(relay({}).sessions, null);
    assert.equal(relay({ sessions: "many" }).sessions, null);
  });

  it("voids the list when a row cannot be identified", () => {
    // Not "drops that row": a list missing an entry would read as a session
    // that went away.
    assert.equal(relay({ sessions: [row(), row({ id: "" })] }).sessions, null);
    assert.equal(relay({ sessions: [row(), row({ id: 7 })] }).sessions, null);
    assert.equal(relay({ sessions: [row(), null] }).sessions, null);
    assert.equal(relay({ sessions: [row(), ["conv_b"]] }).sessions, null);
  });

  it("nulls a field it cannot read instead of coercing it", () => {
    const [session] = relay({
      sessions: [
        row({
          label: "",
          status: 3,
          pending: "1",
          elicitationId: "",
          updatedAtMs: Number.NaN,
          costUsd: null,
          maxCostUsd: Infinity,
        }),
      ],
    }).sessions;
    assert.deepEqual(session, {
      id: "conv_a",
      label: null,
      status: null,
      // "1" is not a count: a coerced 1 would claim a prompt is pending.
      pending: null,
      elicitationId: null,
      updatedAtMs: null,
      costUsd: null,
      maxCostUsd: null,
      budgetUnreadable: false,
    });
  });

  it("keeps a genuine zero as zero", () => {
    // The mirror of the rule above: `0` pending is a FACT (nothing is waiting)
    // and must not be flattened into the same "unknown" as an unreadable one.
    const [session] = relay({ sessions: [row({ pending: 0, costUsd: 0 })] }).sessions;
    assert.equal(session.pending, 0);
    assert.equal(session.costUsd, 0);
  });

  it("keeps the budget-readability flag three-valued too", () => {
    // The last field that was still collapsing to `false`. It states whether
    // the row's budget PARSED, so "we could not read that statement" must not
    // arrive as "it parsed fine" — the shell divides by the limit next to it.
    const flag = (value) =>
      relay({ sessions: [row({ budgetUnreadable: value })] }).sessions[0].budgetUnreadable;
    assert.equal(flag(true), true);
    assert.equal(flag(false), false);
    assert.equal(flag("false"), null);
    assert.equal(flag("yes"), null);
    assert.equal(flag({}), null);
    assert.equal(flag(0), null);
    assert.equal(relay({ sessions: [{ id: "conv_a" }] }).sessions[0].budgetUnreadable, null);
  });
});
