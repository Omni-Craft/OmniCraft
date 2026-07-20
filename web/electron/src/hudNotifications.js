// Desktop notifications driven by the HUD's feed report: WHICH moments deserve
// an OS toast, and the discipline that each one fires exactly once.
//
// Pure and dependency-free so `node --test` can exercise every branch without
// an Electron runtime — the clock and the delivery are injected. main.js owns
// the Notification object; this owns the judgement.
//
// Two rules shape all of it, both inherited from the rest of the monitor:
//
//   1. **Uncertainty never fires.** A report that could not be read, tallies
//      that are a floor rather than a total, a snapshot that stopped
//      refreshing, or a list that left sessions out — none of those may assert
//      that something NEW happened. A notification is a claim; we only make
//      claims the feed proves.
//   2. **A fired event must be able to fire again.** The "already notified"
//      sets are rebuilt from the current report on every trustworthy pass, so a
//      condition the feed proves is over drops out and the same session can
//      notify again the next time it happens. A set that only ever grew would
//      leave the user permanently deaf to a recurring prompt.
//
// SCOPE — the notifications live for as long as the HUD window does. The HUD's
// renderer is the only thing in the shell with an authenticated session to poll
// the feed; a HUD hidden by its visibility mode keeps polling (the modes
// `hide()`, they do not `close()`), so hidden still notifies. With the HUD
// turned OFF nobody watches the feed and nothing is notified.

"use strict";

/** Fraction of the declared budget that earns a warning. */
const DEFAULT_BUDGET_THRESHOLD = 0.8;

/**
 * How long a session may sit at the same `updated_at` before it is called
 * stuck.
 *
 * HEURISTIC, and deliberately generous. There is no watchdog on the server to
 * borrow from — the runner's own timers are in-process and the server only ever
 * sees their terminal result — so "stuck" is inferred here, from a clock the
 * shell can read. `updated_at` is the last WRITE to the conversation (it moves
 * when an item is appended), which means a long turn that persists nothing —
 * one slow tool call, one long model response — looks identical to a wedged
 * one. Fifteen minutes is well past any turn that is merely slow, so a false
 * "parada" costs the user a toast they can ignore, and a real one is caught.
 */
const DEFAULT_STUCK_AFTER_MS = 15 * 60 * 1000;

/** Statuses that count as work in flight — the only ones that can be stuck. */
const IN_FLIGHT_STATUSES = ["running", "launching"];

/**
 * Statuses that mean the session stopped.
 *
 * `unknown` is NOT here, and that is the whole point: a server restart or a
 * second replica turns `running` into `unknown` without anything having
 * finished, so a test like `status !== "running"` would announce a completion
 * that never happened. The same rule already governs the web layer's own
 * turn-end notifications (`web/src/lib/idleTransitions.ts`).
 */
const TERMINAL_STATUSES = ["idle", "failed"];

/** Nothing observed yet: no baseline, nothing notified. */
const EMPTY_NOTIFICATION_STATE = Object.freeze({
  seeded: false,
  previous: [],
  permissions: [],
  budgets: [],
  stuck: [],
});

/** Coerce anything that is not a finite number to `null` — never to `0`. */
function finite(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The per-session detail the report PROVES, or `null` when it proves nothing.
 *
 * The bar is the same one `awaitingSignature` sets for re-expanding the HUD,
 * plus the list itself: events are per-session, so a list that left sessions
 * out (`truncated`, or any unresolved row) cannot tell a session that stopped
 * from one that was merely dropped from the page.
 *
 * @param {unknown} report
 * @returns {object[] | null}
 */
function trustedSessions(report) {
  if (report === null || typeof report !== "object") return null;
  // Every flag is read as three-valued: `null` is the edge saying it could not
  // read that field, and an unread field is not a satisfied condition. Hence
  // `=== false` rather than `!== true` — the difference between "the server
  // says the list is complete" and "we have no idea whether it is".
  if (report.readable !== true || report.exact !== true) return null;
  if (report.stale !== false || report.truncated !== false) return null;
  if (report.unresolved !== 0) return null;
  const sessions = report.sessions;
  if (!Array.isArray(sessions)) return null;
  const ids = new Set();
  for (const session of sessions) {
    if (session === null || typeof session !== "object" || Array.isArray(session)) return null;
    if (typeof session.id !== "string" || session.id.length === 0) return null;
    // Duplicate ids would make "the row for this session" ambiguous, and every
    // decision below is keyed by it.
    if (ids.has(session.id)) return null;
    ids.add(session.id);
  }
  return sessions;
}

/** What to call a session on screen; its id is the last resort, never blank. */
function sessionLabel(session) {
  return typeof session.label === "string" && session.label.length > 0 ? session.label : session.id;
}

/** In-app route the click opens, matching the SPA's conversation path. */
function sessionPath(session) {
  return `/c/${session.id}`;
}

/**
 * The parked prompt this session is blocked on, identified by the pair that
 * makes it unique.
 *
 * A session whose prompt index says "waiting" but whose prompt could not be
 * READ has no identity, so it raises nothing: with no id there is no way to
 * tell the next poll's prompt from this one, and a notification per poll is
 * worse than none. The HUD still shows the row as needing attention.
 *
 * @returns {{sessionId: string, elicitationId: string} | null}
 */
function pendingPermission(session) {
  const pending = finite(session.pending);
  if (pending === null || pending <= 0) return null;
  const elicitationId = session.elicitationId;
  if (typeof elicitationId !== "string" || elicitationId.length === 0) return null;
  return { sessionId: session.id, elicitationId };
}

/**
 * Whether the session crossed the budget threshold, as a fraction of a limit
 * somebody DECLARED.
 *
 * There is exactly one legitimate percentage on this feed and this is it. No
 * declared `maxCostUsd` means no denominator, so no percentage and no alert —
 * and a row flagged `budget_unreadable` has a budget nobody could read, which
 * is not a number to divide by either.
 *
 * @returns {{sessionId: string, threshold: number, ratio: number, maxCostUsd: number} | null}
 */
function budgetCrossing(session, threshold) {
  // Only a PROVEN `false` clears the way: the flag says whether the limit
  // could be read, so a flag we could not read ourselves is not permission to
  // divide by the number next to it.
  if (session.budgetUnreadable !== false) return null;
  const max = finite(session.maxCostUsd);
  const cost = finite(session.costUsd);
  if (max === null || max <= 0 || cost === null) return null;
  const ratio = cost / max;
  if (ratio < threshold) return null;
  return { sessionId: session.id, threshold, ratio, maxCostUsd: max };
}

/**
 * Whether the session is in a stuck EPISODE right now.
 *
 * The age is measured against the feed's OWN `generated_at`, not this
 * machine's clock: `updated_at` is written by the server, and a desktop a
 * quarter of an hour off the server would otherwise manufacture (or hide) a
 * stall out of pure clock skew. Two timestamps from the same clock subtract
 * cleanly. No `generated_at` means no measurable age, so nothing is claimed.
 *
 * The episode is identified by the SESSION, not by the timestamp it froze at.
 * Keying on `updated_at` would end the episode on any write — including one
 * that leaves the session still fifteen minutes behind — and fire a second
 * toast on the spot. An episode ends when the feed shows the session moving
 * again (or no longer in flight), and only then may a later freeze notify.
 *
 * @returns {{sessionId: string} | null}
 */
function stuckEpisode(session, generatedAtMs, stuckAfterMs) {
  if (generatedAtMs === null) return null;
  if (!IN_FLIGHT_STATUSES.includes(session.status)) return null;
  // Only a PROVEN zero: an unreadable prompt index may be hiding the very
  // prompt that explains why nothing is moving.
  if (finite(session.pending) !== 0) return null;
  const updatedAtMs = finite(session.updatedAtMs);
  if (updatedAtMs === null) return null;
  if (generatedAtMs - updatedAtMs <= stuckAfterMs) return null;
  return { sessionId: session.id, silentForMs: generatedAtMs - updatedAtMs };
}

/** Whether an entry with the same identity is already in `entries`. */
function includesEntry(entries, entry, fields) {
  return entries.some((known) => fields.every((field) => known[field] === entry[field]));
}

/** The status the last trustworthy report gave this session, else `null`. */
function priorStatus(previous, id) {
  const found = previous.find((entry) => entry.id === id);
  return found === undefined ? null : found.status;
}

/** Round a ratio to whole percent for display; an overspend reads over 100. */
function percentText(ratio) {
  return `${Math.round(ratio * 100)}%`;
}

/** A dollar amount, plainly. The pure module owns no locale machinery. */
function usdText(value) {
  return `US$ ${value.toFixed(2)}`;
}

/** Whole minutes, for the "stopped N minutes ago" line. */
function minutesText(ms) {
  return `${Math.floor(ms / 60000)} min`;
}

/** Normalize whatever we were handed back into a state object we can read. */
function normalizeState(state) {
  if (state === null || typeof state !== "object" || Array.isArray(state)) {
    return EMPTY_NOTIFICATION_STATE;
  }
  const list = (value) => (Array.isArray(value) ? value : []);
  return {
    seeded: state.seeded === true,
    previous: list(state.previous),
    permissions: list(state.permissions),
    budgets: list(state.budgets),
    stuck: list(state.stuck),
  };
}

/**
 * The events one feed report earns, and the state the next one is judged
 * against.
 *
 * The FIRST trustworthy report only seeds: every condition already true when
 * the HUD opened is recorded as if it had been announced. Otherwise turning the
 * HUD on would fire a burst of toasts for an approval that has been pending
 * since this morning and a session that went idle yesterday. From the second
 * report on, only conditions that are new against that baseline notify.
 *
 * An untrustworthy report changes NOTHING — not the baseline either. That
 * matters for completions: a `running` we saw, then a gap we could not read,
 * then an `idle` is still a real finish, and diffing against the last report we
 * could actually believe is what preserves it.
 *
 * @param {object} input
 * @param {unknown} input.state Previous state, or null on the first call.
 * @param {unknown} input.report The HUD's feed report. Its `generatedAtMs` is
 *   the SERVER's clock, and the only one ages are measured against.
 * @param {number} [input.budgetThreshold] Fraction of the declared budget.
 * @param {number} [input.stuckAfterMs] Silence that counts as stuck.
 * @returns {{events: object[], state: object}}
 */
function detectHudNotifications({
  state,
  report,
  budgetThreshold = DEFAULT_BUDGET_THRESHOLD,
  stuckAfterMs = DEFAULT_STUCK_AFTER_MS,
}) {
  const base = normalizeState(state);
  const sessions = trustedSessions(report);
  // Not knowing is not an event, and it is not a baseline either.
  if (sessions === null) return { events: [], state: base };
  const generatedAtMs = finite(report.generatedAtMs);

  const events = [];
  const permissions = [];
  const budgets = [];
  const stuck = [];
  const previous = [];
  const presentIds = new Set(sessions.map((session) => session.id));

  for (const session of sessions) {
    const label = sessionLabel(session);
    const navigatePath = sessionPath(session);
    previous.push({
      id: session.id,
      status: typeof session.status === "string" ? session.status : null,
    });

    const permission = pendingPermission(session);
    if (permission !== null) {
      permissions.push(permission);
      if (
        base.seeded &&
        !includesEntry(base.permissions, permission, ["sessionId", "elicitationId"])
      ) {
        events.push({
          category: "permission",
          sessionId: session.id,
          title: label,
          body: "Precisa da sua decisão para continuar.",
          navigatePath,
        });
      }
    }

    const budget = budgetCrossing(session, budgetThreshold);
    if (budget !== null) {
      if (!includesEntry(budgets, budget, ["sessionId", "threshold"])) {
        budgets.push({ sessionId: budget.sessionId, threshold: budget.threshold });
      }
      if (base.seeded && !includesEntry(base.budgets, budget, ["sessionId", "threshold"])) {
        events.push({
          category: "budget",
          sessionId: session.id,
          title: label,
          body: `Já gastou ${percentText(budget.ratio)} do orçamento declarado de ${usdText(budget.maxCostUsd)}.`,
          navigatePath,
        });
      }
    }

    const episode = stuckEpisode(session, generatedAtMs, stuckAfterMs);
    if (episode !== null) {
      stuck.push({ sessionId: episode.sessionId });
      if (base.seeded && !includesEntry(base.stuck, episode, ["sessionId"])) {
        events.push({
          category: "stuck",
          sessionId: session.id,
          title: label,
          body: `Sem atividade registrada há ${minutesText(episode.silentForMs)} e sem pedir nada.`,
          navigatePath,
        });
      }
    }

    // Completion is a TRANSITION, so it needs no "already notified" set: the
    // next report's baseline already holds the terminal status, and only a new
    // run can produce another `running` to leave.
    if (base.seeded && TERMINAL_STATUSES.includes(session.status)) {
      if (IN_FLIGHT_STATUSES.includes(priorStatus(base.previous, session.id))) {
        events.push({
          category: "completion",
          sessionId: session.id,
          title: label,
          body: session.status === "failed" ? "A sessão falhou." : "A sessão terminou.",
          navigatePath,
        });
      }
    }
  }

  // Absence only counts as departure when the observation was COMPLETE.
  //
  // Every "already notified" set above is rebuilt from the rows in hand, so a
  // session that is not in them has its state dropped and may notify again.
  // That is right when the feed accounted for everything — the session really
  // is gone. It is wrong when the feed said it could not carry every settled
  // row: the session may have finished and simply not fitted, and treating
  // that as departure would re-announce it the moment it came back.
  //
  // Note what this does NOT do: it never suppresses an event for a row that IS
  // present. A row in hand is proven, whatever the collection around it left
  // out — an incomplete observation can only cost us a notification, never
  // manufacture one.
  if (report.observationComplete !== true) {
    for (const known of base.permissions) {
      if (!presentIds.has(known.sessionId)) permissions.push(known);
    }
    for (const known of base.stuck) {
      if (!presentIds.has(known.sessionId)) stuck.push(known);
    }
    for (const known of base.previous) {
      if (!presentIds.has(known.id)) previous.push(known);
    }
  }

  // A crossed threshold is announced ONCE for as long as the session lives,
  // so its identity is carried forward rather than rebuilt from the current
  // reading. Spend is a running total the server reconciles, and it wobbles
  // around a limit: 79% → 80% → 79% → 80% is one crossing, not two, and
  // rebuilding the set from "who is over right now" would toast on every
  // wobble. This is exactly where budget differs from a pending prompt or a
  // stall — those really are conditions that end and can genuinely recur.
  //
  // It is still pruned, just by the right thing: the session leaving the feed,
  // or its declared budget going away. Both mean the old identity describes
  // nothing that exists.
  for (const known of base.budgets) {
    if (!presentIds.has(known.sessionId)) {
      // Same rule as above: only a complete observation may call this gone.
      if (report.observationComplete === true) continue;
    } else if (!declaresBudget(sessions, known.sessionId)) {
      continue;
    }
    if (!includesEntry(budgets, known, ["sessionId", "threshold"])) budgets.push(known);
  }

  return { events, state: { seeded: true, previous, permissions, budgets, stuck } };
}

/** Whether the session still declares a limit a percentage could come from. */
function declaresBudget(sessions, sessionId) {
  const session = sessions.find((entry) => entry.id === sessionId);
  if (session === undefined || session.budgetUnreadable !== false) return false;
  const max = finite(session.maxCostUsd);
  return max !== null && max > 0;
}

/**
 * Stateful wrapper: hold the state between reports and hand each event to
 * `deliver`.
 *
 * Still dependency-free — only the delivery is injected — so the wiring is as
 * testable as the decision. There is no clock here on purpose: every age this
 * module judges is measured between two of the SERVER's own timestamps, so the
 * desktop's clock never enters into it.
 *
 * @param {object} deps
 * @param {(event: object) => void} deps.deliver Show one notification.
 * @param {number} [deps.budgetThreshold]
 * @param {number} [deps.stuckAfterMs]
 * @param {(message: string, error: unknown) => void} [deps.onError]
 */
function createHudNotifier({ deliver, budgetThreshold, stuckAfterMs, onError }) {
  const reportError = onError ?? (() => {});
  let state = EMPTY_NOTIFICATION_STATE;

  return {
    /** A feed report arrived. Fires whatever it proves is new. */
    observe(report) {
      const result = detectHudNotifications({
        state,
        report,
        budgetThreshold,
        stuckAfterMs,
      });
      state = result.state;
      for (const event of result.events) {
        // One notification failing must not swallow the ones behind it.
        try {
          deliver(event);
        } catch (err) {
          reportError("could not show a HUD notification", err);
        }
      }
    },

    /**
     * The HUD is gone. Forget the baseline so the next window seeds its own —
     * a conversation that finished while nobody was watching is not news the
     * moment the HUD comes back.
     */
    reset() {
      state = EMPTY_NOTIFICATION_STATE;
    },

    /** Test/introspection seam. */
    snapshot() {
      return state;
    },
  };
}

module.exports = {
  DEFAULT_BUDGET_THRESHOLD,
  DEFAULT_STUCK_AFTER_MS,
  EMPTY_NOTIFICATION_STATE,
  IN_FLIGHT_STATUSES,
  TERMINAL_STATUSES,
  detectHudNotifications,
  createHudNotifier,
};
