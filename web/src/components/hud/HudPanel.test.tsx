// Tests for the floating HUD panel (`/hud`).
//
// Mocked at one seam only — `authenticatedFetch`, the network call — so the
// raw wire payload flows through the REAL fetch + parse path the app uses.
// That's deliberate: most of what's asserted here (a null is unknown, an
// unrecognized status doesn't break, an unreadable feed isn't an all-clear)
// lives exactly at that parse boundary.
//
// `approve` is mocked so the verdict POST can be inspected — specifically that
// it targets the elicitation's OWN session, which for a sub-agent prompt is a
// child of the row it renders on.

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { HudPanel } from "./HudPanel";
import { parseMonitorFeed } from "@/hooks/useMonitorFeed";
import * as identity from "@/lib/identity";
import * as sessionsApi from "@/lib/sessionsApi";

vi.mock("@/lib/identity", async (importActual) => ({
  ...(await importActual<typeof import("@/lib/identity")>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/sessionsApi", () => ({ approve: vi.fn() }));

/** Make the next feed read answer with this raw body. */
function serveFeed(body: unknown) {
  vi.mocked(identity.authenticatedFetch).mockResolvedValue({
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response);
}

/** Make the next feed read fail with an HTTP status. */
function serveStatus(status: number) {
  vi.mocked(identity.authenticatedFetch).mockResolvedValue({
    ok: false,
    status,
    statusText: "Bad Request",
    json: async () => ({}),
  } as unknown as Response);
}

/** A raw `GET /v1/monitor/sessions` body, as the server sends it. */
function wireFeed(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: 1_700_000_000,
    host_id: null,
    sessions: [],
    counts: wireCounts(),
    truncated: false,
    degraded: [],
    ...overrides,
  };
}

/**
 * A raw `counts` object, contract-complete. Tests that omit a field are
 * testing the strict edge, and pass a bare literal instead.
 */
function wireCounts(overrides: Record<string, unknown> = {}) {
  return { active: 0, awaiting: 0, unknown: 0, omitted: 0, partial: false, ...overrides };
}

/** A raw monitor session row. */
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
    updated_at: 1_700_000_000,
    cost_usd: 0.42,
    degraded: [],
    ...overrides,
    // The server sends the same spend in both places, so the fixture does too
    // — a test that nulls `cost_usd` must not leave a stale figure in `usage`.
    usage:
      overrides.usage ??
      wireUsage({ cost_usd: "cost_usd" in overrides ? overrides.cost_usd : 0.42 }),
  };
}

/**
 * A raw `usage` object: local counters, and no budget. Tests that want a
 * denominator pass `budget` explicitly — the shape's default is deliberately
 * "no budget", because that is the case the UI must render without a bar.
 */
function wireUsage(overrides: Record<string, unknown> = {}) {
  return {
    source: "local_counter",
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: 0.42,
    budget: null,
    ...overrides,
  };
}

function renderPanel(props: Partial<React.ComponentProps<typeof HudPanel>> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <HudPanel onExpandedChange={() => {}} {...props} />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

/** Wait for the first feed to land, then expand the panel. */
async function expand() {
  await waitFor(() => expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("Carregando"));
  fireEvent.click(screen.getByTestId("hud-pill"));
  return screen.findByTestId("hud-body");
}

beforeEach(() => {
  vi.mocked(identity.authenticatedFetch).mockReset();
  vi.mocked(sessionsApi.approve).mockReset();
  vi.mocked(sessionsApi.approve).mockResolvedValue({ queued: true } as never);
});
afterEach(cleanup);

describe("HudPanel — collapsed pill", () => {
  it("shows the feed's own counts", async () => {
    serveFeed(
      wireFeed({ counts: wireCounts({ active: 3, awaiting: 2 }), sessions: [wireSession()] }),
    );
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("3 ativas · 2 aguardando"),
    );
  });

  it("calls the shell bridge on expand and collapse", async () => {
    serveFeed(wireFeed());
    const onExpandedChange = vi.fn();
    renderPanel({ onExpandedChange });
    fireEvent.click(await screen.findByTestId("hud-pill"));
    expect(onExpandedChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByTestId("hud-pill"));
    expect(onExpandedChange).toHaveBeenLastCalledWith(false);
  });
});

describe("HudPanel — expanded list", () => {
  it("lists the sessions with their project and state", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 2, awaiting: 0 }),
        sessions: [
          wireSession({ session_id: "conv_1", project: "omnicraft" }),
          wireSession({ session_id: "conv_2", project: "outro", status: "launching" }),
        ],
      }),
    );
    renderPanel();
    await expand();
    const rows = screen.getAllByTestId("hud-session");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("omnicraft");
    expect(rows[0]).toHaveTextContent("em execução");
    expect(rows[1]).toHaveTextContent("outro");
    expect(rows[1]).toHaveTextContent("iniciando");
  });

  it("highlights a session that is waiting on a human", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 1 }),
        sessions: [
          wireSession({
            status: "waiting",
            pending_elicitations_count: 1,
            pending_elicitation: {
              id: "elic_1",
              session_id: "conv_1",
              kind: "permission",
              summary: "Rodar `rm -rf build/`?",
            },
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-waiting", "true");
    expect(row).toHaveTextContent("aguardando você");
    expect(within(row).getByTestId("session-state-badge")).toHaveAttribute(
      "data-state",
      "awaiting",
    );
    expect(within(row).getByTestId("approval-card")).toHaveTextContent("Rodar `rm -rf build/`?");
  });

  it("posts the verdict to the elicitation's own session, not the row's", async () => {
    // The prompt belongs to a sub-agent CHILD; the parent row is where a human
    // sees it, but the parked Future lives on the child.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 1 }),
        sessions: [
          wireSession({
            session_id: "conv_parent",
            status: "waiting",
            pending_elicitations_count: 1,
            pending_elicitation: {
              id: "elic_child",
              session_id: "conv_child",
              kind: "permission",
              summary: "Escrever em src/main.py?",
            },
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    fireEvent.click(screen.getByRole("button", { name: "Aprovar" }));
    await waitFor(() => expect(sessionsApi.approve).toHaveBeenCalled());
    expect(sessionsApi.approve).toHaveBeenCalledWith("conv_child", "elic_child", {
      action: "accept",
    });
  });

  it("says a pending prompt is unreadable rather than dropping it", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 1 }),
        sessions: [
          wireSession({
            status: "waiting",
            pending_elicitations_count: 2,
            pending_elicitation: null,
            degraded: ["pending_elicitation_unreadable"],
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-prompt-unreadable")).toHaveTextContent("não pôde ser lido");
    expect(screen.queryByTestId("approval-card")).toBeNull();
  });
});

describe("HudPanel — degraded feeds are never read as all-clear", () => {
  it("reports an unreadable feed instead of 'nada em execução'", async () => {
    serveFeed(wireFeed({ sessions: [], degraded: ["internal_error"] }));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
    );
    await expand();
    expect(screen.getByTestId("hud-unreadable")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("says the list is partial when the feed is truncated", async () => {
    serveFeed(
      wireFeed({ truncated: true, counts: wireCounts({ active: 50, awaiting: 0 }), sessions: [] }),
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId("hud-pill")).toHaveTextContent("parcial"));
    await expand();
    expect(screen.getByTestId("hud-truncated")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("never paints zeroed counts before a feed has been read", async () => {
    // Disabled (identity still resolving) is the same epistemic state as a
    // first read in flight: we do not know, so we must not say "0 · 0".
    serveFeed(wireFeed());
    renderPanel({ enabled: false });
    expect(screen.getByTestId("hud-pill")).toHaveTextContent("Carregando…");
    expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("0 ativas");
    fireEvent.click(screen.getByTestId("hud-pill"));
    expect(screen.getByTestId("hud-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("shows the empty state only for a feed that is genuinely clean", async () => {
    serveFeed(wireFeed());
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-empty")).toBeInTheDocument();
  });
});

describe("HudPanel — unknowns render neutral", () => {
  it("renders null liveness and null cost as unknown, never as offline or zero", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        sessions: [wireSession({ runner_online: null, host_online: null, cost_usd: null })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).getByTestId("hud-runner")).toHaveAttribute("data-tone", "unknown");
    expect(within(row).getByTestId("hud-runner")).toHaveTextContent("runner: desconhecido");
    expect(within(row).getByTestId("hud-host")).toHaveAttribute("data-tone", "unknown");
    expect(within(row).getByTestId("hud-cost")).toHaveTextContent("custo: —");
    expect(row).not.toHaveTextContent("offline");
    expect(row).not.toHaveTextContent("US$ 0.00");
  });

  it("keeps a false liveness distinct from an unknown one", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        sessions: [wireSession({ runner_online: false, host_online: null })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).getByTestId("hud-runner")).toHaveAttribute("data-tone", "down");
    expect(within(row).getByTestId("hud-host")).toHaveAttribute("data-tone", "unknown");
  });

  it("does not break on a status outside the enum", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        sessions: [wireSession({ status: "hibernating", degraded: ["status_unreadable"] })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-status", "unknown");
    expect(within(row).getByTestId("hud-session-status")).toHaveTextContent("estado desconhecido");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "o estado desta sessão não pôde ser lido",
    );
  });
});

describe("HudPanel — a local counter is not a quota", () => {
  /** One row with the given `usage`, expanded and ready to assert on. */
  async function usageRow(usage: Record<string, unknown>, row: Record<string, unknown> = {}) {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1 }),
        sessions: [wireSession({ usage: wireUsage(usage), ...row })],
      }),
    );
    renderPanel();
    await expand();
    return screen.getByTestId("hud-session");
  }

  it("shows a session without a budget as an absolute number, with no bar", async () => {
    const row = await usageRow({ total_tokens: 12_345, cost_usd: 1.5, budget: null });
    expect(within(row).getByTestId("hud-usage")).toHaveAttribute("data-has-budget", "false");
    expect(within(row).getByTestId("hud-tokens")).toHaveTextContent("tokens: 12.345");
    expect(within(row).getByTestId("hud-cost")).toHaveTextContent("US$ 1,50");
    // The whole point: no denominator means no bar, no ramp, no percentage.
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(within(row).queryByRole("progressbar")).toBeNull();
    expect(row).not.toHaveTextContent("%");
    expect(within(row).getByTestId("hud-no-budget")).toBeInTheDocument();
  });

  it("never turns token counts into a percentage", async () => {
    // A big round number is exactly what invites "80% of a 1M window". There
    // is no window on the wire, so there is no percentage on screen.
    const row = await usageRow({ total_tokens: 800_000, input_tokens: 700_000, budget: null });
    expect(row).not.toHaveTextContent("%");
    expect(within(row).queryByRole("progressbar")).toBeNull();
  });

  it("says the counters are a local total rather than an allowance", async () => {
    const row = await usageRow({ total_tokens: 10 });
    expect(within(row).getByTestId("hud-usage")).toHaveTextContent("contador local, não cota");
  });

  it("renders an unrecorded token bucket as unknown, never as zero", async () => {
    const row = await usageRow({ total_tokens: null, cost_usd: null }, { cost_usd: null });
    expect(within(row).getByTestId("hud-tokens")).toHaveAttribute("data-tone", "unknown");
    expect(within(row).getByTestId("hud-tokens")).toHaveTextContent("tokens: —");
    expect(row).not.toHaveTextContent("tokens: 0");
  });
});

describe("HudPanel — a budget is the only thing a percentage may divide by", () => {
  async function budgetRow(costUsd: number | null, maxCostUsd: number | null) {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1 }),
        sessions: [
          wireSession({
            cost_usd: costUsd,
            usage: wireUsage({
              cost_usd: costUsd,
              budget: { max_cost_usd: maxCostUsd, thresholds_usd: [], source: "agent_spec" },
            }),
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    return screen.getByTestId("hud-session");
  }

  it("draws the bar at the real ratio of spend to the declared limit", async () => {
    const row = await budgetRow(1.25, 5);
    const gauge = within(row).getByTestId("hud-budget-gauge");
    expect(gauge).toHaveAttribute("data-percent", "25");
    expect(gauge).toHaveAttribute("aria-valuenow", "25");
    expect(gauge).toHaveTextContent("25% de US$ 5,00");
  });

  it.each([
    [1, 10, "ok", "dentro"],
    [7.5, 10, "warning", "perto do limite"],
    [9.5, 10, "critical", "no limite"],
  ])("ramps %s of %s to %s", async (cost, max, level, phrase) => {
    const row = await budgetRow(cost, max);
    const gauge = within(row).getByTestId("hud-budget-gauge");
    expect(gauge).toHaveAttribute("data-level", level);
    // Colour is never the only carrier: the level is also written out.
    expect(gauge).toHaveTextContent(phrase);
    expect(gauge).toHaveAttribute("aria-label", expect.stringContaining(phrase));
  });

  it("reports an overspend as over 100% instead of a bar that stops at full", async () => {
    const row = await budgetRow(12, 10);
    const gauge = within(row).getByTestId("hud-budget-gauge");
    expect(gauge).toHaveAttribute("data-percent", "120");
    expect(gauge).toHaveAttribute("data-level", "critical");
  });

  it("refuses a percentage when the budget exists but the spend is unknown", async () => {
    const row = await budgetRow(null, 5);
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(within(row).getByTestId("hud-budget-no-spend")).toHaveTextContent("desconhecido");
  });

  it("refuses a budget the server could not settle, rather than drawing it", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, partial: true }),
        sessions: [wireSession({ degraded: ["budget_unreadable"] })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(within(row).getByTestId("hud-budget-unreadable")).toBeInTheDocument();
    // And it must not read as "no budget" — the session has one.
    expect(within(row).queryByTestId("hud-no-budget")).toBeNull();
  });

  it("lets the slug win over a limit sent alongside it", async () => {
    // The server never sends both. If one ever did, a gauge would claim we
    // know the limit while the same row says we don't — and the row would
    // carry a bar and the words "sem barra" at once.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, partial: true }),
        sessions: [
          wireSession({
            degraded: ["budget_unreadable"],
            usage: wireUsage({
              cost_usd: 1,
              budget: { max_cost_usd: 10, thresholds_usd: [], source: "agent_spec" },
            }),
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(within(row).queryByRole("progressbar")).toBeNull();
    expect(row).not.toHaveTextContent("10%");
    expect(within(row).getByTestId("hud-budget-unreadable")).toBeInTheDocument();
  });

  it("keeps the ARIA state inside the range it declares when overspent", async () => {
    const row = await budgetRow(12, 10);
    const gauge = within(row).getByTestId("hud-budget-gauge");
    expect(gauge).toHaveAttribute("aria-valuemax", "100");
    expect(gauge).toHaveAttribute("aria-valuenow", "100");
    // The real figure is not lost — it rides on valuetext, as it does visibly.
    expect(gauge).toHaveAttribute("aria-valuetext", expect.stringContaining("120%"));
  });
});

describe("parseMonitorFeed — a budget is taken whole or not at all", () => {
  /** One row whose `usage.budget` is the given raw value. */
  async function budgetPayload(budget: unknown, usage: Record<string, unknown> = {}) {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1 }),
        sessions: [
          wireSession({ cost_usd: 1, usage: wireUsage({ cost_usd: 1, budget, ...usage }) }),
        ],
      }),
    );
    renderPanel();
    await expand();
    return screen.getByTestId("hud-session");
  }

  it("degrades on a budget that is not even an object, instead of dropping it", async () => {
    const row = await budgetPayload("hostil");
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    // Silently vanishing would read as "this session has no budget".
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "o limite não pôde ser lido",
    );
    expect(within(row).queryByTestId("hud-no-budget")).toBeNull();
  });

  it("refuses a usable limit that arrives with a rotten threshold", async () => {
    const row = await budgetPayload({
      max_cost_usd: 5,
      thresholds_usd: [1, "dois"],
      source: "agent_spec",
    });
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(row).not.toHaveTextContent("20%");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "o limite não pôde ser lido",
    );
  });

  it("refuses a threshold the real gate could never have enforced", async () => {
    // `cost_budget` rejects a checkpoint outside `(0, max)` at build time, so
    // this declaration never ran anywhere. Every number in it parses, which is
    // exactly why the check has to be about coherence, not shape.
    const row = await budgetPayload({
      max_cost_usd: 5,
      thresholds_usd: [10],
      source: "agent_spec",
    });
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(row).not.toHaveTextContent("20%");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "o limite não pôde ser lido",
    );
  });

  it("refuses a budget whose provenance is not the literal we know", async () => {
    const row = await budgetPayload({
      max_cost_usd: 5,
      thresholds_usd: [],
      source: "provider_quota",
    });
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(row).not.toHaveTextContent("20%");
  });

  it("refuses the whole usage object when its provenance does not check out", async () => {
    // If we cannot identify the payload we cannot call it a local counter,
    // and we certainly cannot divide by a budget riding inside it.
    const row = await budgetPayload(
      { max_cost_usd: 5, thresholds_usd: [], source: "agent_spec" },
      { source: "provider_quota", total_tokens: 99 },
    );
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(within(row).getByTestId("hud-tokens")).toHaveTextContent("tokens: —");
    expect(row).not.toHaveTextContent("tokens: 99");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "parte da contagem de tokens não pôde ser lida",
    );
  });

  it("refuses a non-positive limit instead of dividing by it", async () => {
    const row = await budgetPayload({ max_cost_usd: 0, thresholds_usd: [], source: "agent_spec" });
    expect(within(row).queryByTestId("hud-budget-gauge")).toBeNull();
    expect(row).not.toHaveTextContent("Infinity");
    expect(row).not.toHaveTextContent("NaN");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent(
      "o limite não pôde ser lido",
    );
  });
});

describe("parseMonitorFeed — wire normalization", () => {
  it("keeps nulls as unknown and maps an unknown status", () => {
    const feed = parseMonitorFeed(
      wireFeed({
        sessions: [wireSession({ status: "???", runner_online: null, cost_usd: null })],
      }),
    );
    expect(feed.sessions[0].status).toBe("unknown");
    expect(feed.sessions[0].runnerOnline).toBeNull();
    expect(feed.sessions[0].costUsd).toBeNull();
  });

  it("flags an internal_error feed as unreadable", () => {
    expect(parseMonitorFeed(wireFeed({ degraded: ["internal_error"] })).unreadable).toBe(true);
    expect(parseMonitorFeed(wireFeed()).unreadable).toBe(false);
  });

  it("survives a garbage body without throwing — and without inventing calm", () => {
    const feed = parseMonitorFeed(null);
    expect(feed.sessions).toEqual([]);
    // NOT {active: 0, awaiting: 0}: zeros here would let any malformed
    // response render as "nothing needs you".
    expect(feed.counts).toBeNull();
    expect(feed.unreadable).toBe(true);
  });
});

// ── Cross-review blockers ────────────────────────────────────────────
// Each test below pins one way the HUD could assert something it does not
// know. They are written to fail if the corresponding guard is removed.

describe("HudPanel — a payload we cannot read is never an all-clear", () => {
  it("refuses to print counts the payload did not carry", async () => {
    // Feed shaped right but `counts` missing: a fail-open parser would
    // default it to zeros and the pill would read "0 aguardando" — an
    // all-clear derived from an absence.
    serveFeed({ ...wireFeed(), counts: undefined });
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Contagens ilegíveis"),
    );
    expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("0 aguardando");
    await expand();
    expect(screen.getByTestId("hud-counts-unreadable")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("treats a non-numeric count as unknown rather than coercing it", async () => {
    serveFeed(wireFeed({ counts: { active: 3, awaiting: "muitas" } }));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Contagens ilegíveis"),
    );
  });

  it("treats a body that is not a feed as unreadable, not as an empty account", async () => {
    serveFeed("<html>gateway timeout</html>");
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
    );
    await expand();
    expect(screen.getByTestId("hud-unreadable")).toHaveTextContent("não quer dizer que nada");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("treats a missing sessions array as unreadable", async () => {
    serveFeed({ ...wireFeed(), sessions: undefined });
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
    );
  });

  it("flags rows it had to drop instead of silently shortening the list", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 2, awaiting: 0, unknown: 0, omitted: 0 }),
        sessions: [wireSession(), { agent_name: "sem id" }],
      }),
    );
    renderPanel();
    await expand();
    expect(screen.getAllByTestId("hud-session")).toHaveLength(1);
    expect(
      within(screen.getByTestId("hud-degraded")).getByText(/linha do feed/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });
});

describe("HudPanel — every degraded slug counts, known or not", () => {
  it.each([
    "scan_truncated",
    "liveness_partial",
    "permissions_unavailable",
    "agent_names_unavailable",
    "child_sessions_unavailable",
    "pending_elicitations_unavailable",
    "attention_rescue_unavailable",
    "attention_rescue_truncated",
  ])("surfaces %s on an otherwise empty feed", async (slug) => {
    serveFeed(wireFeed({ degraded: [slug] }));
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-degraded")).toBeInTheDocument();
    // The whole point: an empty list plus a degradation is NOT "tudo tranquilo".
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("treats a slug this build has never seen as a degradation too", async () => {
    // Forward-compat: a new server slug must not be silently dropped by an
    // old client, which would turn a partial answer into a confident one.
    serveFeed(wireFeed({ degraded: ["quantum_flux_unavailable"] }));
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("quantum_flux_unavailable");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("marks the counts themselves partial when the scan was cut", async () => {
    serveFeed(
      wireFeed({
        truncated: true,
        degraded: ["scan_truncated"],
        counts: wireCounts({ active: 9, awaiting: 1, unknown: 0, omitted: 0, partial: true }),
      }),
    );
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("piso, não total"),
    );
  });
});

describe("HudPanel — staleness is visible", () => {
  it("marks the snapshot stale once it stops being refreshed", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ active: 4, awaiting: 1, unknown: 0, omitted: 0 }) }));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    const ui = (nowMs?: number) => (
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <HudPanel onExpandedChange={() => {}} nowMs={nowMs} />
        </TooltipProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(ui());
    await waitFor(() => expect(screen.getByTestId("hud-pill")).toHaveTextContent("4 ativas"));
    expect(screen.queryByTestId("hud-stale")).toBeNull();

    // Same snapshot, much later clock: without a staleness check the HUD keeps
    // presenting an old reading as if it were current.
    rerender(ui(Date.now() + 60_000));
    expect(screen.getByTestId("hud-stale")).toBeInTheDocument();
    expect(screen.getByTestId("hud-panel")).toHaveAttribute("data-stale", "true");
  });

  it("keeps the last numbers on screen when a poll fails, but marks them stale", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ active: 2, awaiting: 1, unknown: 0, omitted: 0 }) }));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <HudPanel onExpandedChange={() => {}} />
        </TooltipProvider>
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("hud-pill")).toHaveTextContent("2 ativas"));

    vi.mocked(identity.authenticatedFetch).mockRejectedValue(new Error("network down"));
    await client.refetchQueries({ queryKey: ["monitor-feed"] });
    await waitFor(() => expect(screen.getByTestId("hud-stale")).toBeInTheDocument());
    // The numbers stay (better than blanking) — but never unlabelled.
    expect(screen.getByTestId("hud-pill")).toHaveTextContent("2 ativas");
  });
});

describe("HudPanel — a failed verdict is visible and retryable", () => {
  function servePendingFeed() {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 1, unknown: 0, omitted: 0 }),
        sessions: [
          wireSession({
            status: "waiting",
            pending_elicitations_count: 1,
            pending_elicitation: {
              id: "elic_1",
              session_id: "conv_1",
              kind: "permission",
              summary: "Rodar `rm -rf build/`?",
            },
          }),
        ],
      }),
    );
  }

  it("shows the failure and puts the buttons back when the resolve POST fails", async () => {
    servePendingFeed();
    vi.mocked(sessionsApi.approve).mockRejectedValue(new Error("503 Service Unavailable"));
    renderPanel();
    await expand();
    fireEvent.click(screen.getByRole("button", { name: "Aprovar" }));

    // Silently swallowing this leaves an agent blocked while the HUD implies
    // the prompt was answered.
    await waitFor(() => expect(screen.getByTestId("hud-resolve-error")).toBeInTheDocument());
    expect(screen.getByTestId("hud-resolve-error")).toHaveTextContent("503 Service Unavailable");
    expect(screen.getByRole("button", { name: "Aprovar" })).toBeEnabled();
  });

  it("clears the error when the retry is submitted", async () => {
    servePendingFeed();
    vi.mocked(sessionsApi.approve).mockRejectedValueOnce(new Error("503 Service Unavailable"));
    renderPanel();
    await expand();
    fireEvent.click(screen.getByRole("button", { name: "Aprovar" }));
    await waitFor(() => expect(screen.getByTestId("hud-resolve-error")).toBeInTheDocument());

    vi.mocked(sessionsApi.approve).mockResolvedValue({ queued: true } as never);
    fireEvent.click(screen.getByRole("button", { name: "Aprovar" }));
    await waitFor(() => expect(screen.queryByTestId("hud-resolve-error")).toBeNull());
  });
});

describe("HudPanel — the new feed shape", () => {
  it("reports unknown-status and cap-omitted sessions instead of a clean total", async () => {
    // active excludes unknown, and omitted rows are counted but not carried:
    // printing only "N ativas · M aguardando" would present a partial answer
    // as a complete one.
    serveFeed(
      wireFeed({
        truncated: true,
        counts: wireCounts({ active: 3, awaiting: 1, unknown: 2, omitted: 5 }),
        sessions: [wireSession()],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("3 ativas");
      expect(pill).toHaveTextContent("1 aguardando");
      expect(pill).toHaveTextContent("2 desconhecidas");
      expect(pill).toHaveTextContent("+5 fora da lista");
    });
    await expand();
    expect(screen.getByTestId("hud-truncated")).toHaveTextContent("5 fora da lista");
  });

  it("renders `unknown` status without breaking the row", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 0, awaiting: 0, unknown: 1, omitted: 0 }),
        sessions: [wireSession({ status: "unknown", degraded: ["status_unknown"] })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-status", "unknown");
    expect(within(row).getByTestId("hud-session-status")).toHaveTextContent("estado desconhecido");
  });

  it("preserves the server's human-need ordering", async () => {
    // The feed already ranks blocked → failed → active → idle. Re-sorting by
    // updated_at would bury the session that needs a human under fresher noise.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 3, awaiting: 1, unknown: 0, omitted: 0 }),
        sessions: [
          wireSession({
            session_id: "blocked",
            project: "bloqueada",
            status: "waiting",
            updated_at: 1,
          }),
          wireSession({ session_id: "failed", project: "falhou", status: "failed", updated_at: 2 }),
          wireSession({ session_id: "running", project: "rodando", updated_at: 9_999 }),
        ],
      }),
    );
    renderPanel();
    await expand();
    expect(screen.getAllByTestId("hud-session").map((r) => r.dataset.sessionId)).toEqual([
      "blocked",
      "failed",
      "running",
    ]);
  });

  it("reports a rejected host_id filter as an error, not as an empty feed", async () => {
    serveStatus(400);
    renderPanel({ hostId: "host_inexistente" });
    // The hook retries once before giving up, so allow for its backoff.
    await waitFor(
      () => expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
      { timeout: 5_000 },
    );
    await expand();
    expect(screen.getByTestId("hud-unreadable")).toHaveTextContent("filtro de host foi recusado");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("does not paint a null runner as offline", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0, unknown: 0, omitted: 0 }),
        sessions: [wireSession({ runner_online: null, host_online: false })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).getByTestId("hud-runner")).toHaveAttribute("data-tone", "unknown");
    expect(within(row).getByTestId("hud-runner")).not.toHaveTextContent("offline");
    // …while a confirmed-false one still reads offline.
    expect(within(row).getByTestId("hud-host")).toHaveAttribute("data-tone", "down");
  });
});

// ── Second cross-review round: the envelope itself is untrusted ──────
// The class of defect these pin: a payload that is MISSING something is not a
// payload that says "nothing". Each case below would, with a fail-open parser,
// render as a calm board.

describe("HudPanel — a partial envelope is not a clean one", () => {
  it("refuses an incomplete counts object instead of printing its two clean numbers", async () => {
    // The exact shape the review flagged: an old/partial body carrying only
    // `active` and `awaiting`. Reading it would put "0 ativas · 0 aguardando"
    // on screen off a payload that never described the whole matching set.
    serveFeed(wireFeed({ sessions: [], counts: { active: 0, awaiting: 0 } }));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Contagens ilegíveis"),
    );
    expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("0 aguardando");
    await expand();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("refuses counts whose `partial` flag is missing", async () => {
    // Without it we cannot tell a total from a floor — and a floor read as a
    // total is the whole failure this feed exists to avoid.
    serveFeed(wireFeed({ counts: { active: 4, awaiting: 0, unknown: 0, omitted: 0 } }));
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Contagens ilegíveis"),
    );
    expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("4 ativas");
  });

  it("does not let a missing `truncated` pass as a complete list", async () => {
    serveFeed({ ...wireFeed(), truncated: undefined });
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-truncated")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("degrades on a missing `generated_at` rather than reading the feed as sound", async () => {
    serveFeed({ ...wireFeed(), generated_at: undefined });
    renderPanel();
    await expand();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("hora do feed");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("treats a missing `degraded` list as unreadable, not as nothing-went-wrong", async () => {
    // That list is the only record of what the server could not resolve;
    // absent, every other number on the payload is unqualified.
    serveFeed({ ...wireFeed(), degraded: undefined });
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
    );
    await expand();
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });

  it("reads the 200-with-internal_error answer as 'não sei', never as 'nada rodando'", async () => {
    // The route no longer emits 500: an unexpected failure comes back as this
    // exact body. Empty sessions here say nothing about what is running.
    serveFeed(
      wireFeed({
        sessions: [],
        counts: wireCounts({ partial: true }),
        truncated: true,
        degraded: ["internal_error"],
      }),
    );
    renderPanel();
    await waitFor(() =>
      expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
    );
    await expand();
    expect(screen.getByTestId("hud-unreadable")).toHaveTextContent("não quer dizer que nada");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });
});

describe("HudPanel — an unreadable prompt index is a '?', never a '0'", () => {
  it("shows a null count as unknown instead of an unblocked session", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, partial: true }),
        sessions: [
          wireSession({
            pending_elicitations_count: null,
            pending_elicitation: null,
            degraded: ["pending_elicitations_unknown"],
          }),
        ],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-pending-unknown", "true");
    expect(within(row).getByTestId("hud-pending-unknown")).toHaveTextContent(
      "aprovações pendentes: ?",
    );
    expect(within(row).getByTestId("hud-pending-unknown-detail")).toHaveTextContent(
      "Não dá para saber se esta sessão está esperando por você",
    );
    // No count means no claim either way — the badge may not say "awaiting",
    // and nothing here may say "0 pendentes".
    expect(within(row).getByTestId("session-state-badge")).not.toHaveAttribute(
      "data-state",
      "awaiting",
    );
    expect(row).not.toHaveTextContent("0 aprovação");
  });

  it("treats a count outside its domain as unknown rather than accepting it", async () => {
    // `-1` is not a small number of prompts; taken at face value it renders as
    // a session nobody is waiting on, hiding a possible block on a human.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1 }),
        sessions: [wireSession({ pending_elicitations_count: -1 })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-pending-unknown", "true");
    expect(within(row).getByTestId("hud-pending-unknown")).toBeInTheDocument();
    // And the feed's own tallies stop claiming to be a total.
    expect(screen.getByTestId("hud-pill")).toHaveTextContent("piso, não total");
    expect(screen.getByTestId("hud-counts-partial")).toBeInTheDocument();
  });

  it.each([1.5, "2", true])(
    "treats a malformed count (%s) as unknown rather than coercing it",
    async (value) => {
      serveFeed(
        wireFeed({
          counts: wireCounts({ active: 1 }),
          sessions: [wireSession({ pending_elicitations_count: value })],
        }),
      );
      renderPanel();
      await expand();
      expect(screen.getByTestId("hud-session")).toHaveAttribute("data-pending-unknown", "true");
    },
  );
});

describe("HudPanel — partial counts are shown as a floor", () => {
  it("says the tallies are a floor when the server marks them partial", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 3, awaiting: 2, omitted: 4, partial: true }),
        degraded: ["attention_rescue_truncated"],
        sessions: [wireSession()],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      // "≥" and not a bare number: 2 is the least that may need a human.
      expect(pill).toHaveTextContent("≥3 ativas");
      expect(pill).toHaveTextContent("≥2 aguardando");
      expect(pill).toHaveTextContent("piso, não total");
    });
    await expand();
    expect(screen.getByTestId("hud-counts-partial")).toHaveTextContent("piso");
    // `omitted` now also covers attention-bearing sessions the server could
    // not resolve, so they are named as possibly needing a human.
    expect(screen.getByTestId("hud-truncated")).toHaveTextContent("podem precisar de você");
  });

  it("keeps plain totals plain when nothing degraded", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ active: 3, awaiting: 2 }), sessions: [] }));
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("3 ativas · 2 aguardando");
      expect(pill).not.toHaveTextContent("≥");
      expect(pill).not.toHaveTextContent("piso");
    });
  });
});

describe("HudPanel — the host filter's 503", () => {
  it("reports an unverifiable host as a readable error, not an empty feed", async () => {
    // No host registry to check against: the server refuses rather than
    // answering with a feed scoped to nothing.
    serveStatus(503);
    renderPanel({ hostId: "host_qualquer" });
    await waitFor(
      () => expect(screen.getByTestId("hud-pill")).toHaveTextContent("Feed indisponível"),
      { timeout: 5_000 },
    );
    await expand();
    expect(screen.getByTestId("hud-unreadable")).toHaveTextContent("registro de hosts");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });
});

describe("HudPanel — verdict state is scoped to its session", () => {
  function serveTwinPrompts() {
    // Elicitation ids are unique per session, so two sessions can legitimately
    // carry the same one.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 2, awaiting: 2 }),
        sessions: ["conv_a", "conv_b"].map((id) =>
          wireSession({
            session_id: id,
            project: id,
            status: "waiting",
            pending_elicitations_count: 1,
            pending_elicitation: {
              id: "elic_1",
              session_id: id,
              kind: "permission",
              summary: `Aprovar em ${id}?`,
            },
          }),
        ),
      }),
    );
  }

  it("does not mark another session's prompt answered", async () => {
    serveTwinPrompts();
    renderPanel();
    await expand();
    const rows = screen.getAllByTestId("hud-session");
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Aprovar" }));
    await waitFor(() =>
      expect(sessionsApi.approve).toHaveBeenCalledWith("conv_a", "elic_1", {
        action: "accept",
      }),
    );
    // The other session is still blocked and must still be answerable.
    expect(
      within(screen.getAllByTestId("hud-session")[1]).getByRole("button", { name: "Aprovar" }),
    ).toBeInTheDocument();
  });

  it("shows a failed verdict only on the row that failed", async () => {
    serveTwinPrompts();
    vi.mocked(sessionsApi.approve).mockRejectedValue(new Error("503 Service Unavailable"));
    renderPanel();
    await expand();
    const rows = screen.getAllByTestId("hud-session");
    fireEvent.click(within(rows[0]).getByRole("button", { name: "Aprovar" }));
    await waitFor(() =>
      expect(within(rows[0]).getByTestId("hud-resolve-error")).toBeInTheDocument(),
    );
    expect(within(rows[1]).queryByTestId("hud-resolve-error")).toBeNull();
  });
});

// ── Third round: a clean envelope says nothing about the rows in it ──

describe("HudPanel — one degraded row makes the whole pill a floor", () => {
  it("stops presenting counts as a total when a row carries a degradation", async () => {
    // Envelope is spotless — `partial: false`, `degraded: []` — but one row
    // says its status is unresolved. Those counts cannot describe a session
    // whose state nobody could resolve, so they are a floor.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 2, awaiting: 1 }),
        degraded: [],
        sessions: [wireSession({ status: "unknown", degraded: ["status_unknown"] })],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("≥2 ativas");
      expect(pill).toHaveTextContent("piso, não total");
    });
    await expand();
    expect(screen.getByTestId("hud-counts-partial")).toBeInTheDocument();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent(
      "o estado desta sessão não está registrado",
    );
  });

  it("does the same for a row whose prompt index could not be read", async () => {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        degraded: [],
        sessions: [
          wireSession({
            pending_elicitations_count: null,
            pending_elicitation: null,
            degraded: [],
          }),
        ],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      // "0 aguardando" here would be an all-clear built on a row that may well
      // be blocked on a human.
      expect(pill).toHaveTextContent("≥0 aguardando");
      expect(pill).toHaveTextContent("piso, não total");
    });
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(within(row).getByTestId("hud-pending-unknown")).toHaveTextContent(
      "aprovações pendentes: ?",
    );
    expect(within(row).getByTestId("hud-pending-unknown-detail")).toBeInTheDocument();
  });
});

// The shell owns the visibility modes ("hide when idle", "only on attention")
// but has no authenticated session of its own: this panel is its only source
// for what the feed says. What it reports therefore has to carry the same
// uncertainty the panel renders — a floor is not a total, and an unreadable
// feed is not an idle one — or the shell would hide the HUD on numbers nobody
// could resolve.
describe("HudPanel — what it reports to the shell", () => {
  it("reports a fully-resolved feed as readable and exact", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ active: 2, awaiting: 1, unknown: 0, omitted: 0 }) }));
    const onFeedReport = vi.fn();
    renderPanel({ onFeedReport });

    await waitFor(() =>
      expect(onFeedReport).toHaveBeenCalledWith(
        expect.objectContaining({ readable: true, exact: true, active: 2, awaiting: 1 }),
      ),
    );
  });

  it("names WHICH sessions are waiting, not just how many", async () => {
    // The shell re-expands the HUD only for attention the user has not already
    // dismissed, and a count cannot tell one prompt from another. The list is
    // named the way the tallies count it — a parked prompt — so the shell can
    // check it accounts for every awaiting session.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 2, awaiting: 1 }),
        sessions: [
          wireSession({ session_id: "conv_busy" }),
          wireSession({ session_id: "conv_blocked", pending_elicitations_count: 2 }),
        ],
      }),
    );
    const onFeedReport = vi.fn();
    renderPanel({ onFeedReport });

    await waitFor(() =>
      expect(onFeedReport).toHaveBeenCalledWith(
        expect.objectContaining({ awaiting: 1, awaitingIds: ["conv_blocked"] }),
      ),
    );
  });

  it("reports an unbuildable feed as unreadable, not as zeros", async () => {
    serveFeed(wireFeed({ degraded: ["internal_error"], counts: null }));
    const onFeedReport = vi.fn();
    renderPanel({ onFeedReport });

    // The feed answered; anything still readable=true would be the panel
    // reporting the pre-fetch blank as an answer.
    await waitFor(() => expect(screen.getByTestId("hud-pill")).toHaveTextContent("indisponível"));
    expect(onFeedReport.mock.lastCall?.[0]).toMatchObject({ readable: false, exact: false });
  });

  it("reports partial tallies as inexact, so a floor is never read as idle", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ partial: true, unknown: 1 }) }));
    const onFeedReport = vi.fn();
    renderPanel({ onFeedReport });

    // Sessions the feed couldn't resolve travel too — an all-zero active count
    // with one unresolved session is not an idle machine.
    await waitFor(() =>
      expect(onFeedReport).toHaveBeenCalledWith(
        expect.objectContaining({ readable: true, exact: false, unresolved: 1 }),
      ),
    );
  });

  it("follows the shell when IT expands the HUD (attention showed up)", async () => {
    serveFeed(wireFeed({ counts: wireCounts({ awaiting: 1 }), sessions: [wireSession()] }));
    let notify: ((expanded: boolean) => void) | null = null;
    renderPanel({
      subscribeExpanded: (callback) => {
        notify = callback;
        return () => {};
      },
    });
    await waitFor(() => expect(screen.getByTestId("hud-pill")).not.toHaveTextContent("Carregando"));
    expect(screen.queryByTestId("hud-body")).not.toBeInTheDocument();

    // The shell resized its window and says so; the panel must render the
    // state the window is actually in.
    notify!(true);
    expect(await screen.findByTestId("hud-body")).toBeInTheDocument();
  });
});

// ── Fourth round: no optional field is read outside the accumulator ──
// Same class of defect, one level deeper. Each field below used to degrade to
// `null` in silence, so a feed with `partial: false` and `degraded: []` could
// carry a row that had lost information while the pill printed its tallies as
// a total. Every case here serves a SPOTLESS envelope and a value that is
// present but malformed — the only honest reading is a floor.

describe("HudPanel — a field lost inside a row still makes the counts a floor", () => {
  /** Clean envelope, one row, whatever the case wants broken on it. */
  function serveRow(overrides: Record<string, unknown>) {
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        degraded: [],
        sessions: [wireSession(overrides)],
      }),
    );
  }

  async function expectFloor() {
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("≥1 ativas");
      expect(pill).toHaveTextContent("piso, não total");
    });
    await expand();
    expect(screen.getByTestId("hud-counts-partial")).toBeInTheDocument();
  }

  it("records a prompt whose `kind` is malformed instead of defaulting it", async () => {
    // Absent `kind` legitimately means "unknown"; a `kind` of the wrong shape
    // is information we lost, and must not land on that same default quietly.
    serveRow({
      status: "waiting",
      pending_elicitations_count: 1,
      pending_elicitation: { id: "elic_1", session_id: "conv_1", kind: 7, summary: "Seguir?" },
    });
    renderPanel();
    await expectFloor();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("aprovação pendente");
  });

  it("records a prompt whose `summary` is malformed instead of blanking it", async () => {
    serveRow({
      status: "waiting",
      pending_elicitations_count: 1,
      pending_elicitation: {
        id: "elic_1",
        session_id: "conv_1",
        kind: "permission",
        summary: { texto: "Seguir?" },
      },
    });
    renderPanel();
    await expectFloor();
  });

  it("records a malformed label rather than rendering the row as unnamed", async () => {
    serveRow({ project: 42 });
    renderPanel();
    await expectFloor();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("identificação desta sessão");
  });

  it("records a malformed `updated_at` instead of treating it as never-updated", async () => {
    serveRow({ updated_at: "ontem" });
    renderPanel();
    await expectFloor();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("última atividade");
  });

  it("keeps a legitimately absent optional field free of any fault", async () => {
    // The distinction the accumulator exists to preserve: ABSENT is the server
    // having nothing to say, and must stay a plain total.
    serveFeed(
      wireFeed({
        counts: wireCounts({ active: 1, awaiting: 0 }),
        degraded: [],
        sessions: [wireSession({ project: null, title: null, cost_usd: null, host_online: null })],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("1 ativas · 0 aguardando");
      expect(pill).not.toHaveTextContent("≥");
      expect(pill).not.toHaveTextContent("piso");
    });
    await expand();
    expect(screen.queryByTestId("hud-counts-partial")).toBeNull();
    expect(screen.queryByTestId("hud-degraded")).toBeNull();
  });
});

describe("HudPanel — a malformed host_id is not a feed about nothing", () => {
  it("records it instead of silently forgetting which host the counts describe", async () => {
    serveFeed(
      wireFeed({
        host_id: 7,
        counts: wireCounts({ active: 2, awaiting: 1 }),
        degraded: [],
        sessions: [],
      }),
    );
    renderPanel();
    await waitFor(() => {
      const pill = screen.getByTestId("hud-pill");
      expect(pill).toHaveTextContent("≥2 ativas");
      expect(pill).toHaveTextContent("piso, não total");
    });
    await expand();
    expect(screen.getByTestId("hud-degraded")).toHaveTextContent("a que host");
    expect(screen.queryByTestId("hud-empty")).toBeNull();
  });
});
