// Tests for the shell's desktop notifications (src/hudNotifications.js), run
// with `node --test` (no extra deps).
//
// Two bug classes are under the microscope, and they pull in opposite
// directions:
//
//   * **The lie.** Announcing something that did not happen — a completion
//     invented out of `running` → `unknown` (a server restart, another
//     replica), a budget percentage derived from a limit nobody declared, a
//     "stopped" verdict on a session whose prompt index we could not read.
//   * **The deafness.** Notifying once and then never again, so a prompt that
//     comes back, a budget that is crossed again, or a session that freezes a
//     second time passes in silence.
//
// So nearly every case here is a pair: it fires once, and then it either stays
// quiet or comes back — on purpose.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_STUCK_AFTER_MS,
  detectHudNotifications,
  createHudNotifier,
} = require("../src/hudNotifications");

const NOW = 1_700_000_000_000;

/** A session row as the preload sanitizes it. */
function session(overrides = {}) {
  return {
    id: "conv_a",
    label: "Projeto A",
    status: "running",
    pending: 0,
    elicitationId: null,
    updatedAtMs: NOW,
    costUsd: null,
    maxCostUsd: null,
    budgetUnreadable: false,
    ...overrides,
  };
}

/**
 * A fully-resolved report: readable, exact, fresh, complete.
 *
 * `generatedAtMs` is the SERVER's clock, and the second element of a step (see
 * `run`) moves it — the desktop's own clock never enters into any decision.
 */
function report(sessions, overrides = {}) {
  return {
    readable: true,
    exact: true,
    stale: false,
    truncated: false,
    observationComplete: true,
    generatedAtMs: NOW,
    active: sessions.length,
    awaiting: 0,
    unresolved: 0,
    awaitingIds: [],
    sessions,
    ...overrides,
  };
}

/**
 * Feed a sequence of reports and collect what each one fired.
 *
 * @param {Array<object|[object, number]>} steps A report, or a report plus the
 *   server clock reading it was generated at.
 * @returns {{fired: object[][], state: object}}
 */
function run(steps) {
  let state = null;
  const fired = [];
  for (const step of steps) {
    const [next, generatedAtMs] = Array.isArray(step) ? step : [step, null];
    const shaped = generatedAtMs === null ? next : { ...next, generatedAtMs };
    const result = detectHudNotifications({ state, report: shaped });
    state = result.state;
    fired.push(result.events);
  }
  return { fired, state };
}

/** Categories fired at each step, for terse assertions. */
function categories(fired) {
  return fired.map((events) => events.map((event) => event.category));
}

describe("the first report", () => {
  it("seeds the baseline and announces nothing", () => {
    // A HUD that just opened onto an approval pending since this morning, a
    // session that finished yesterday and one frozen for an hour must not
    // dump three toasts on the user.
    const { fired } = run([
      report([
        session({ id: "a", pending: 1, elicitationId: "el_1" }),
        session({ id: "b", status: "idle" }),
        session({ id: "c", updatedAtMs: NOW - 2 * DEFAULT_STUCK_AFTER_MS }),
        session({ id: "d", costUsd: 9, maxCostUsd: 10 }),
      ]),
    ]);
    assert.deepEqual(fired[0], []);
  });

  it("is the first TRUSTWORTHY report that seeds, not the first arrival", () => {
    // A HUD that opens onto an unreadable feed has learned nothing. If that
    // counted as the baseline, the first report it could actually read would
    // fire the whole backlog as if it had just happened.
    const { fired } = run([
      report([session({ pending: 1, elicitationId: "el_1" })], { readable: false }),
      report([session({ pending: 1, elicitationId: "el_1" })]),
      report([session({ pending: 1, elicitationId: "el_1" })]),
    ]);
    assert.deepEqual(categories(fired), [[], [], []]);
  });

  it("keeps the seeded conditions quiet on the reports after it", () => {
    const pending = report([session({ pending: 1, elicitationId: "el_1" })]);
    const { fired } = run([pending, pending, pending]);
    assert.deepEqual(categories(fired), [[], [], []]);
  });
});

describe("permission", () => {
  it("fires once for a prompt that appears, then stays quiet", () => {
    const idle = report([session()]);
    const asking = report([session({ pending: 1, elicitationId: "el_1" })]);
    const { fired } = run([idle, asking, asking]);
    assert.deepEqual(categories(fired), [[], ["permission"], []]);
    assert.equal(fired[1][0].sessionId, "conv_a");
    assert.equal(fired[1][0].navigatePath, "/c/conv_a");
  });

  it("fires again for a NEW prompt after the first was answered", () => {
    const { fired } = run([
      report([session()]),
      report([session({ pending: 1, elicitationId: "el_1" })]),
      report([session()]),
      report([session({ pending: 1, elicitationId: "el_2" })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["permission"], [], ["permission"]]);
  });

  it("fires again when the SAME prompt id comes back after being resolved", () => {
    // The "already notified" set is rebuilt from the feed, so a condition the
    // feed proves is over cannot leave the user deaf to its return.
    const asking = report([session({ pending: 1, elicitationId: "el_1" })]);
    const { fired } = run([report([session()]), asking, report([session()]), asking]);
    assert.deepEqual(categories(fired), [[], ["permission"], [], ["permission"]]);
  });

  it("says nothing when the prompt index says waiting but names no prompt", () => {
    // No id, no identity: a notification per poll would be worse than none.
    const { fired } = run([
      report([session()]),
      report([session({ pending: 1, elicitationId: null })]),
      report([session({ pending: 1, elicitationId: null })]),
    ]);
    assert.deepEqual(categories(fired), [[], [], []]);
  });

  it("says nothing when the prompt index could not be read", () => {
    const { fired } = run([
      report([session()]),
      report([session({ pending: null, elicitationId: "el_1" })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });
});

describe("completion", () => {
  it("fires on running → idle", () => {
    const { fired } = run([report([session()]), report([session({ status: "idle" })])]);
    assert.deepEqual(categories(fired), [[], ["completion"]]);
    assert.match(fired[1][0].body, /terminou/);
  });

  it("fires on running → failed, and says so", () => {
    const { fired } = run([report([session()]), report([session({ status: "failed" })])]);
    assert.deepEqual(categories(fired), [[], ["completion"]]);
    assert.match(fired[1][0].body, /falhou/);
  });

  it("fires on launching → idle", () => {
    const { fired } = run([
      report([session({ status: "launching" })]),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["completion"]]);
  });

  it("does NOT fire on running → unknown", () => {
    // The trap: a server restart or a second replica turns `running` into
    // `unknown`, and nothing finished. `status !== "running"` would announce it.
    const { fired } = run([
      report([session()]),
      report([session({ status: "unknown" })]),
      report([session({ status: "unknown" })]),
    ]);
    assert.deepEqual(categories(fired), [[], [], []]);
  });

  it("does NOT fire on unknown → idle", () => {
    // Coming back from an unreadable status is not a transition we witnessed.
    const { fired } = run([
      report([session({ status: "unknown" })]),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("does not re-announce a session that stays idle", () => {
    const { fired } = run([
      report([session()]),
      report([session({ status: "idle" })]),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["completion"], []]);
  });

  it("fires again for a second run", () => {
    const { fired } = run([
      report([session()]),
      report([session({ status: "idle" })]),
      report([session()]),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["completion"], [], ["completion"]]);
  });

  it("still fires across a gap the shell could not read", () => {
    // The baseline only advances on reports we believe, so `running`, then a
    // blind spot, then `idle` is still a real finish.
    const { fired } = run([
      report([session()]),
      report([session({ status: "idle" })], { readable: false }),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], [], ["completion"]]);
  });
});

describe("budget", () => {
  it("fires once at the threshold and does not repeat as spend rises", () => {
    const { fired } = run([
      report([session({ costUsd: 1, maxCostUsd: 10 })]),
      report([session({ costUsd: 8, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], []]);
    assert.match(fired[1][0].body, /80%/);
    assert.match(fired[1][0].body, /US\$ 10\.00/);
  });

  it("says nothing without a declared limit, however much was spent", () => {
    // No denominator, no percentage — the rule the whole usage surface runs on.
    // The spend starts UNKNOWN so the baseline has nothing to have seen: this
    // has to hold because there is no budget, not because it was seeded.
    const { fired } = run([
      report([session({ costUsd: null, maxCostUsd: null })]),
      report([session({ costUsd: 9_999, maxCostUsd: null })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("says nothing when the limit could not be read", () => {
    const { fired } = run([
      report([session({ costUsd: 1, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10, budgetUnreadable: true })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("says nothing when we cannot even read whether the limit was readable", () => {
    // The flag is the row's own statement about whether its budget parsed.
    // Anything other than a proven `false` is an unread statement, and an
    // unread statement is not permission to divide by the number beside it —
    // cost and limit being present is exactly what makes this tempting.
    for (const flag of [undefined, null, "false", {}, 0]) {
      const { fired } = run([
        report([session({ costUsd: 1, maxCostUsd: 10 })]),
        report([session({ costUsd: 9, maxCostUsd: 10, budgetUnreadable: flag })]),
      ]);
      assert.deepEqual(
        categories(fired),
        [[], []],
        `budgetUnreadable: ${JSON.stringify(flag)} was treated as readable`,
      );
    }
  });

  it("stops carrying the warning once the flag stops proving the budget readable", () => {
    // The carry-forward rule has to read the flag the same way: a row whose
    // readability we cannot establish is not a row still declaring a budget.
    const { fired } = run([
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10, budgetUnreadable: null })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], [], ["budget"]]);
  });

  it("says nothing when the spend is unknown", () => {
    const { fired } = run([
      report([session({ costUsd: 1, maxCostUsd: 10 })]),
      report([session({ costUsd: null, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("reports an overspend above 100%", () => {
    const { fired } = run([
      report([session({ costUsd: 1, maxCostUsd: 10 })]),
      report([session({ costUsd: 12, maxCostUsd: 10 })]),
    ]);
    assert.match(fired[1][0].body, /120%/);
  });

  it("stays quiet while the spend wobbles across the threshold", () => {
    // 79 → 80 → 79 → 80 is ONE crossing. Spend is a running total the server
    // reconciles, so it drifts back and forth around a limit; rebuilding the
    // "already warned" set from whoever is over RIGHT NOW would toast on every
    // wobble. The identity is the session's for as long as the session lives.
    const { fired } = run([
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 8.0, maxCostUsd: 10 })]),
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 8.0, maxCostUsd: 10 })]),
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9.5, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], [], [], [], []]);
  });

  it("warns again when the budget itself goes away and comes back", () => {
    // Not a wobble in the spend: the session stopped declaring a limit at all,
    // so the old warning describes a denominator that no longer exists.
    const { fired } = run([
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: null })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], [], ["budget"]]);
  });

  it("warns again for a session that left the feed and came back", () => {
    const { fired } = run([
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
      report([]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], [], ["budget"]]);
  });
});

describe("stuck", () => {
  const frozenAt = NOW - DEFAULT_STUCK_AFTER_MS - 1000;

  it("stays quiet until the silence passes the threshold", () => {
    const { fired } = run([
      [report([session({ updatedAtMs: NOW })]), NOW],
      [report([session({ updatedAtMs: NOW })]), NOW + DEFAULT_STUCK_AFTER_MS - 1],
      [report([session({ updatedAtMs: NOW })]), NOW + DEFAULT_STUCK_AFTER_MS + 1],
    ]);
    assert.deepEqual(categories(fired), [[], [], ["stuck"]]);
  });

  it("fires once per episode", () => {
    const frozen = report([session({ updatedAtMs: frozenAt })]);
    const { fired } = run([report([session()]), frozen, frozen]);
    assert.deepEqual(categories(fired), [[], ["stuck"], []]);
  });

  it("ends the episode when the session writes again, and can fire anew", () => {
    const { fired } = run([
      [report([session({ updatedAtMs: NOW })]), NOW],
      [report([session({ updatedAtMs: frozenAt })]), NOW],
      // It moved: the episode is over.
      [report([session({ updatedAtMs: NOW })]), NOW],
      // ...and froze again, long enough to count a second time.
      [report([session({ updatedAtMs: NOW })]), NOW + DEFAULT_STUCK_AFTER_MS + 1],
    ]);
    assert.deepEqual(categories(fired), [[], ["stuck"], [], ["stuck"]]);
  });

  it("does not re-announce when the write is itself older than the threshold", () => {
    // A late-arriving write moves `updated_at` without the session catching
    // up: still silent, still the same stall. Keying the episode on the
    // timestamp would have called this a brand-new one and toasted at once.
    const { fired } = run([
      [report([session({ updatedAtMs: NOW })]), NOW],
      [report([session({ updatedAtMs: NOW - 40 * 60_000 })]), NOW],
      [report([session({ updatedAtMs: NOW - 20 * 60_000 })]), NOW],
      [report([session({ updatedAtMs: NOW - 16 * 60_000 })]), NOW],
    ]);
    assert.deepEqual(categories(fired), [[], ["stuck"], [], []]);
  });

  it("measures the silence on the SERVER's clock, not this machine's", () => {
    // A desktop half an hour behind the server would otherwise see every
    // session as freshly written, and a desktop half an hour ahead would
    // report every one of them as stalled.
    const skewed = report([session({ updatedAtMs: NOW })], { generatedAtMs: NOW });
    const { fired } = run([skewed, skewed, skewed]);
    assert.deepEqual(categories(fired), [[], [], []]);
  });

  it("says nothing when the feed did not say when it was generated", () => {
    // No server clock, no measurable age — and an age measured against the
    // desktop's clock is not an age, it is a guess about two machines.
    const { fired } = run([
      report([session({ updatedAtMs: NOW })], { generatedAtMs: null }),
      report([session({ updatedAtMs: frozenAt })], { generatedAtMs: null }),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("says nothing about a session that is waiting on a human", () => {
    // It is not stuck; it is blocked, and the permission event covers that.
    const { fired } = run([
      report([session()]),
      report([session({ updatedAtMs: frozenAt, pending: 1, elicitationId: "el_1" })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["permission"]]);
  });

  it("says nothing when the prompt index could not be read", () => {
    // An unreadable index may be hiding the prompt that explains the silence.
    const { fired } = run([
      report([session()]),
      report([session({ updatedAtMs: frozenAt, pending: null })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("says nothing about a session that is not in flight", () => {
    const { fired } = run([
      report([session({ status: "idle" })]),
      report([session({ status: "idle", updatedAtMs: frozenAt })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("says nothing when the last-activity time is unknown", () => {
    const { fired } = run([report([session()]), report([session({ updatedAtMs: null })])]);
    assert.deepEqual(categories(fired), [[], []]);
  });
});

describe("an uncertain report", () => {
  const before = report([session()]);
  const after = (overrides) =>
    report(
      [
        session({ id: "conv_a", status: "idle" }),
        session({ id: "b", pending: 1, elicitationId: "el_9" }),
      ],
      overrides,
    );

  for (const [name, overrides] of [
    ["unreadable", { readable: false }],
    ["counted as a floor", { exact: false }],
    ["stale", { stale: true }],
    ["truncated", { truncated: true }],
    ["hiding sessions it could not resolve", { unresolved: 2 }],
    // The three-valued half: `null` is the edge saying it could not read that
    // field. An unread flag is not a satisfied condition, and a `!== true`
    // test would have let every one of these through as "fine".
    ["unsure whether it is readable", { readable: null }],
    ["unsure whether its counts are exact", { exact: null }],
    ["unsure whether it is stale", { stale: null }],
    ["unsure whether the list is complete", { truncated: null }],
    ["unsure how many sessions it could not resolve", { unresolved: null }],
  ]) {
    it(`fires nothing when the feed is ${name}`, () => {
      const { fired } = run([before, after(overrides)]);
      assert.deepEqual(categories(fired), [[], []]);
    });
  }

  it("fires nothing when the row list is missing", () => {
    const { fired } = run([before, { ...report([]), sessions: null }]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("fires nothing when a row has no usable id", () => {
    const { fired } = run([before, report([session({ id: "" })])]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("fires nothing when two rows share an id", () => {
    const { fired } = run([
      before,
      report([session({ status: "idle" }), session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], []]);
  });

  it("leaves the baseline untouched, so it does not become the first report", () => {
    // If an unreadable report reset the seed, the next good one would seed
    // again and swallow a real completion.
    const { state } = run([before, report([session()], { readable: false })]);
    assert.equal(state.seeded, true);
    assert.deepEqual(state.previous, [{ id: "conv_a", status: "running" }]);
  });
});

describe("a session that leaves the feed", () => {
  it("drops out of the state, so its return can notify again", () => {
    const asking = report([session({ pending: 1, elicitationId: "el_1" })]);
    const { fired, state } = run([report([session()]), asking, report([]), asking]);
    assert.deepEqual(categories(fired), [[], ["permission"], [], ["permission"]]);
    assert.deepEqual(state.previous.length, 1);
  });

  it("is NOT treated as gone when the feed said it could not carry everything", () => {
    // The server reports whether the settled collection held every session
    // that finished. When it did not, a session missing from the rows may
    // simply not have fitted — and re-announcing its prompt when it turns up
    // again would be a toast built on a gap.
    const asking = report([session({ pending: 1, elicitationId: "el_1" })]);
    const { fired } = run([
      report([session()]),
      asking,
      report([], { observationComplete: false }),
      asking,
    ]);
    assert.deepEqual(categories(fired), [[], ["permission"], [], []]);
  });

  it("keeps a crossed budget through an incomplete observation", () => {
    const { fired } = run([
      report([session({ costUsd: 7.9, maxCostUsd: 10 })]),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
      report([], { observationComplete: false }),
      report([session({ costUsd: 9, maxCostUsd: 10 })]),
    ]);
    assert.deepEqual(categories(fired), [[], ["budget"], [], []]);
  });

  it("still acts on the rows an incomplete observation DID carry", () => {
    // The asymmetry that makes this safe: a row in hand is proven whatever
    // the collection around it left out, so an incomplete observation can
    // only ever cost a notification — it can never manufacture one.
    const { fired } = run([
      report([session({ id: "a" }), session({ id: "b" })]),
      report([session({ id: "a", status: "idle" })], { observationComplete: false }),
    ]);
    assert.deepEqual(categories(fired), [[], ["completion"]]);
  });

  it("preserves the baseline of a session it could not account for", () => {
    // It was running; the feed then could not carry everything and left it
    // out; later it reappears finished. That IS a completion we witnessed.
    const { fired } = run([
      report([session()]),
      report([], { observationComplete: false }),
      report([session({ status: "idle" })]),
    ]);
    assert.deepEqual(categories(fired), [[], [], ["completion"]]);
  });
});

describe("createHudNotifier", () => {
  it("delivers each event and keeps state between reports", () => {
    const delivered = [];
    const notifier = createHudNotifier({
      deliver: (event) => delivered.push(event.category),
    });
    notifier.observe(report([session()]));
    notifier.observe(report([session({ status: "idle" })]));
    notifier.observe(report([session({ status: "idle" })]));
    assert.deepEqual(delivered, ["completion"]);
  });

  it("keeps going when one delivery throws", () => {
    const errors = [];
    const delivered = [];
    let first = true;
    const notifier = createHudNotifier({
      deliver: (event) => {
        if (first) {
          first = false;
          throw new Error("no notification service");
        }
        delivered.push(event.category);
      },
      onError: (message) => errors.push(message),
    });
    notifier.observe(report([session({ id: "a" }), session({ id: "b" })]));
    notifier.observe(
      report([session({ id: "a", status: "idle" }), session({ id: "b", status: "failed" })]),
    );
    assert.equal(errors.length, 1);
    assert.deepEqual(delivered, ["completion"]);
  });

  it("forgets its baseline on reset, so a new HUD re-seeds", () => {
    const delivered = [];
    const notifier = createHudNotifier({
      deliver: (event) => delivered.push(event.category),
    });
    notifier.observe(report([session()]));
    notifier.reset();
    // A finish that happened while no HUD was watching is not news.
    notifier.observe(report([session({ status: "idle" })]));
    assert.deepEqual(delivered, []);
  });
});
