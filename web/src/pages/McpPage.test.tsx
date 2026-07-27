import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { McpPage } from "./McpPage";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  authenticatedFetch: vi.fn(),
}));

const fetchMock = vi.mocked(authenticatedFetch);

// The catalog link needs a router in scope.
const render = (at = "/craftwork/mcps") =>
  rtlRender(
    <MemoryRouter initialEntries={[at]}>
      <McpPage />
    </MemoryRouter>,
  );

const AGENTS = [{ id: "ag_chat", name: "chat" }];

const CONNECTORS = [
  {
    id: "memory",
    title: "Memory",
    description: "Memória em grafo de conhecimento.",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-memory"],
    env_required: [],
  },
  {
    id: "github",
    title: "GitHub",
    description: "Issues e PRs.",
    transport: "stdio",
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-github"],
    env_required: [{ name: "GITHUB_TOKEN" }],
  },
];

function mockBackend(servers: unknown[] = []) {
  fetchMock.mockImplementation((input) => {
    const url = String(input);
    const body = url.includes("/v1/agents/")
      ? { data: servers }
      : url.includes("mcp-catalog")
        ? { connectors: CONNECTORS }
        : { data: AGENTS };
    return Promise.resolve({ ok: true, json: async () => body } as Response);
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  mockBackend();
});
afterEach(cleanup);

describe("McpPage", () => {
  it("fills the form from a catalog card", async () => {
    render();
    fireEvent.click(await screen.findByTestId("mcp-shelf-memory"));

    expect(screen.getByPlaceholderText("ex.: fetch")).toHaveValue("memory");
    expect(screen.getByPlaceholderText("npx")).toHaveValue("npx");
    expect(screen.getByPlaceholderText("-y @modelcontextprotocol/server-fetch")).toHaveValue(
      "-y @modelcontextprotocol/server-memory",
    );
  });

  it("keeps credential-hungry connectors out of the shelf", async () => {
    // Prefilling one would save a server with no token and fail at connect
    // time — the directory installs those, because it can ask for the secret.
    render();
    await screen.findByTestId("mcp-shelf-memory");
    expect(screen.queryByTestId("mcp-shelf-github")).not.toBeInTheDocument();
  });

  it("keeps the directory link inside the shell the reader is in", async () => {
    // Crossing to the other shell would swap the sidebar out mid-task.
    render("/settings/mcps");
    expect(await screen.findByTestId("mcp-directory-link")).toHaveAttribute(
      "href",
      "/settings/connectors",
    );
    cleanup();

    render("/craftwork/mcps");
    expect(await screen.findByTestId("mcp-directory-link")).toHaveAttribute(
      "href",
      "/craftwork/connectors",
    );
  });

  it("counts the servers the agent already has", async () => {
    mockBackend([
      { name: "fetch", transport: "stdio", description: null, url: null, command: "npx", args: [] },
    ]);
    render();
    await waitFor(() => expect(screen.getByText("fetch")).toBeInTheDocument());
    expect(screen.getByText("1")).toBeInTheDocument();
  });
});
