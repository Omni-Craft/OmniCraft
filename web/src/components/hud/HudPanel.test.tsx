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
