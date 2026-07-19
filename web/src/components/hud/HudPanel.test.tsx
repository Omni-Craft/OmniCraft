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

/** A raw `GET /v1/monitor/sessions` body, as the server sends it. */
function wireFeed(overrides: Record<string, unknown> = {}) {
  return {
    generated_at: 1_700_000_000,
    host_id: null,
    sessions: [],
    counts: { active: 0, awaiting: 0 },
    truncated: false,
    degraded: [],
    ...overrides,
  };
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
    serveFeed(wireFeed({ counts: { active: 3, awaiting: 2 }, sessions: [wireSession()] }));
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
        counts: { active: 2, awaiting: 0 },
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
        counts: { active: 1, awaiting: 1 },
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
        counts: { active: 1, awaiting: 1 },
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
        counts: { active: 1, awaiting: 1 },
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
    serveFeed(wireFeed({ truncated: true, counts: { active: 50, awaiting: 0 }, sessions: [] }));
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
        counts: { active: 1, awaiting: 0 },
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
        counts: { active: 1, awaiting: 0 },
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
        counts: { active: 1, awaiting: 0 },
        sessions: [wireSession({ status: "hibernating", degraded: ["status_unreadable"] })],
      }),
    );
    renderPanel();
    await expand();
    const row = screen.getByTestId("hud-session");
    expect(row).toHaveAttribute("data-status", "unknown");
    expect(within(row).getByTestId("hud-session-status")).toHaveTextContent("estado desconhecido");
    expect(within(row).getByTestId("hud-session-degraded")).toHaveTextContent("status_unreadable");
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

  it("survives a garbage body without throwing", () => {
    const feed = parseMonitorFeed(null);
    expect(feed.sessions).toEqual([]);
    expect(feed.counts).toEqual({ active: 0, awaiting: 0 });
    expect(feed.unreadable).toBe(false);
  });
});
