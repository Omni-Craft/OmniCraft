// End-to-end test for the desktop notifications the Electron shell raises.
//
// Everything else about them is unit-tested against hand-built reports, and
// that is exactly the gap this file exists to close: a report a test AUTHORS
// can contain a row the server would never send. The most expensive way to be
// wrong here is to detect a transition the endpoint cannot express — the HUD
// polls the ACTIVE view, and a session that finishes simply stops appearing
// in it, so a detector that waits for `running` → `idle` would wait for ever.
//
// So this drives the real chain, mocked at the network seam only:
//
//   raw `GET /v1/monitor/sessions` body (server semantics)
//     → the app's own parse (`useMonitorFeed`)
//     → the report `HudPanel` hands the shell
//     → the shell's detector (`electron/src/hudNotifications.js`, imported
//       here as the real module)
//
// If any link stops matching the next, this fails — which is the point.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { HudPanel } from "./HudPanel";
import type { HudFeedReport } from "@/lib/hudBridge";
import * as identity from "@/lib/identity";
import { detectHudNotifications } from "../../../electron/src/hudNotifications.js";

vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/identity")>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", () => ({ approve: vi.fn() }));

const GENERATED_AT = 1_700_000_000;

/** A raw `GET /v1/monitor/sessions` body, as the server sends it. */
function wireFeed(sessions: unknown[], overrides: Record<string, unknown> = {}) {
  return {
    generated_at: GENERATED_AT,
    host_id: null,
    sessions,
    counts: { active: 0, awaiting: 0, unknown: 0, omitted: 0, partial: false },
    truncated: false,
    degraded: [],
    ...overrides,
  };
}

/** A raw monitor row. Defaults to a plain running session with no budget. */
function wireSession(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "conv_1",
    agent_name: "research-agent",
    title: "Refatorar o runner",
    project: "omnicraft",
    workspace: "/repo",
    status: "running",
    pending_elicitations_count: 0,
    pending_elicitation: null,
    runner_online: true,
    host_online: true,
    updated_at: GENERATED_AT,
    cost_usd: 0.42,
    degraded: [],
    usage: {
      source: "local_counter",
      input_tokens: null,
      output_tokens: null,
      total_tokens: null,
      cache_read_input_tokens: null,
      cache_creation_input_tokens: null,
      cost_usd: 0.42,
      budget: null,
    },
    ...overrides,
  };
}

/**
 * The URL the panel actually requested, so the grace window is asserted
 * against the wire and not against a constant.
 */
function requestedUrl(call = 0): string {
  return String(vi.mocked(identity.authenticatedFetch).mock.calls[call]?.[0] ?? "");
}

/**
 * Answer a fixture the way `routes/monitor.py` would: settled sessions leave
 * the active view entirely and reappear in `settled` — but only for a caller
 * that asked for a window, and only while they are inside it.
 *
 * This is what makes the test worth having. A mock that echoed the fixture
 * would happily hand the panel an `idle` row in `sessions`, which the real
 * server never sends, and the chain would look fine while the product notified
 * nothing.
 *
 * What it deliberately does NOT reproduce, because a fake server proving
 * itself right is worth nothing: the row cap and its interaction with the
 * settled quota, the ranking, `counts.omitted`, `truncated` and
 * `settled_omitted`. Those are the SERVER's contract and are pinned where the
 * real code runs — `tests/server/routes/test_monitor_feed.py`. What this file
 * proves is the other half: given that contract, the chain from wire to toast
 * holds.
 */
function asServerWouldAnswer(body: Record<string, unknown>, url: string) {
  const grace = Number(
    new URL(url, "http://server").searchParams.get("settled_grace_seconds") ?? 0,
  );
  const generatedAt = body.generated_at as number;
  const rows = body.sessions as Record<string, unknown>[];
  const isSettled = (row: Record<string, unknown>) =>
    row.status === "idle" &&
    row.pending_elicitations_count === 0 &&
    (row.degraded as unknown[]).length === 0;
  // Inside the window is measured on the SERVER's clock against the row's own
  // `updated_at` — the same subtraction `_settled_recently` does.
  const inWindow = (row: Record<string, unknown>) =>
    grace > 0 && typeof row.updated_at === "number" && generatedAt - row.updated_at <= grace;
  return {
    ...body,
    sessions: rows.filter((row) => !isSettled(row)),
    settled: rows.filter((row) => isSettled(row) && inWindow(row)),
    settled_omitted: (body.settled_omitted as number) ?? 0,
  };
}

/**
 * Render the panel over a scripted sequence of raw feed bodies (one per poll)
 * and return every report it handed the shell.
 */
async function reportsFor(bodies: Record<string, unknown>[]): Promise<HudFeedReport[]> {
  let index = 0;
  vi.mocked(identity.authenticatedFetch).mockImplementation(
    async (url: string) =>
      ({
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => asServerWouldAnswer(bodies[Math.min(index++, bodies.length - 1)], url),
      }) as unknown as Response,
  );
  const reports: HudFeedReport[] = [];
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <HudPanel
          onExpandedChange={() => {}}
          onFeedReport={(report) => reports.push(report)}
          nowMs={GENERATED_AT * 1000}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
  /** Reports built from a snapshot that actually landed — not the loading one. */
  const read = () => reports.filter((report) => report.readable);
  for (let poll = 1; poll <= bodies.length; poll += 1) {
    await waitFor(() => expect(read().length).toBeGreaterThanOrEqual(poll));
    if (poll < bodies.length) await client.refetchQueries();
  }
  return reports;
}

/** Run the shell's real detector over a sequence of reports. */
function detect(reports: HudFeedReport[]): string[][] {
  let state: unknown = null;
  return reports.map((report) => {
    const result = detectHudNotifications({ state, report });
    state = result.state;
    return result.events.map((event: { category: string }) => event.category);
  });
}

beforeEach(() => {
  vi.mocked(identity.authenticatedFetch).mockReset();
});
afterEach(cleanup);

describe("a session finishing, end to end", () => {
  it("asks the feed to carry sessions that just settled", async () => {
    // Without this the endpoint answers with the active view, where a
    // finished session is simply absent — and absence is not a fact.
    await reportsFor([wireFeed([wireSession()])]);
    expect(requestedUrl()).toContain("only_active=true");
    expect(requestedUrl()).toContain("settled_grace_seconds=120");
  });

  it("notifies a completion the server expressed as a settled row", async () => {
    const reports = await reportsFor([
      wireFeed([wireSession({ status: "running" })], {
        counts: { active: 1, awaiting: 0, unknown: 0, omitted: 0, partial: false },
      }),
      // What the endpoint returns for that same session once it finishes: an
      // `idle` row, carried ONLY because of the grace window. The tallies stop
      // counting it as active — a settled row was never active.
      wireFeed([wireSession({ status: "idle" })]),
    ]);
    expect(reports.at(-1)?.sessions.at(-1)).toMatchObject({ id: "conv_1", status: "idle" });
    expect(reports.at(-1)?.observationComplete).toBe(true);
    expect(detect(reports).flat()).toEqual(["completion"]);
  });

  it("says nothing about a session that settled before the window opened", async () => {
    // The window is a moment. A session that finished an hour ago is not
    // carried at all, so there is nothing to witness — and nothing is invented
    // from it having gone missing.
    const reports = await reportsFor([
      wireFeed([wireSession({ status: "running" })]),
      wireFeed([wireSession({ status: "idle", updated_at: GENERATED_AT - 3600 })]),
    ]);
    expect(reports.at(-1)?.sessions).toHaveLength(0);
    expect(detect(reports).flat()).toEqual([]);
  });

  it("stops claiming a complete observation when settlements were left out", async () => {
    // `settled_omitted` is the collection's own completeness, and the only
    // field that can answer "did I see every completion?".
    const reports = await reportsFor([
      wireFeed([wireSession({ status: "running" })], { settled_omitted: 3 }),
    ]);
    expect(reports.at(-1)?.observationComplete).toBe(false);
  });

  it("does not show the settled row it observed", async () => {
    // The grace window is for the shell, not for the panel: the HUD answers
    // "what needs me", and finished work does not. Anything else here would
    // be a UX change smuggled in by a notification feature.
    const reports = await reportsFor([
      wireFeed([wireSession({ session_id: "conv_done", status: "idle" }), wireSession()], {
        counts: { active: 1, awaiting: 0, unknown: 0, omitted: 0, partial: false },
      }),
    ]);
    // Observed: both. Listed: only the active one — a settled row never enters
    // the active view, on the wire or on screen.
    expect([...(reports.at(-1)?.sessions ?? [])].map((session) => session.id).sort()).toEqual([
      "conv_1",
      "conv_done",
    ]);
    fireEvent.click(screen.getByTestId("hud-pill"));
    await screen.findByTestId("hud-body");
    const shown = screen.getAllByTestId("hud-session").map((row) => row.dataset.sessionId);
    expect(shown).toEqual(["conv_1"]);
  });

  it("says nothing when the session merely vanishes from the feed", async () => {
    // The other half of the contract: if the row is gone we do NOT get to
    // call it a completion. A row cap, a changed filter and a deleted session
    // look exactly like a finished one.
    const reports = await reportsFor([
      wireFeed([wireSession({ status: "running" })]),
      wireFeed([]),
    ]);
    expect(detect(reports).flat()).toEqual([]);
  });

  it("carries the server's clock, so ages are not measured against this machine", async () => {
    const reports = await reportsFor([wireFeed([wireSession()])]);
    expect(reports.at(-1)?.generatedAtMs).toBe(GENERATED_AT * 1000);
  });
});
