import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConnectorsPage } from "./ConnectorsPage";

const fetchMock = vi.fn();

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (path: string, init?: RequestInit) => fetchMock(path, init),
}));

const CATALOG = {
  connectors: [
    {
      id: "memory",
      title: "Memory",
      emoji: "🧠",
      category: "memória",
      description: "Memória em grafo.",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-memory"],
      env_required: [],
    },
    {
      id: "github",
      title: "GitHub",
      emoji: "🐙",
      category: "dev",
      description: "Issues e pull requests.",
      transport: "stdio",
      command: "npx",
      args: ["-y", "@modelcontextprotocol/server-github"],
      env_required: [{ name: "GITHUB_PERSONAL_ACCESS_TOKEN", label: "Token do GitHub" }],
    },
  ],
};

function jsonOk(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
}

/** Route the page's calls; `installed` seeds the agent's existing servers. */
function wireFetch(installed: { name: string }[] = []) {
  fetchMock.mockImplementation((path: string) => {
    if (path === "/v1/agents") return jsonOk({ data: [{ id: "ag_1", name: "chat" }] });
    if (path === "/v1/mcp-catalog") return jsonOk(CATALOG);
    if (path.endsWith("/test")) return jsonOk({ ok: true, tool_count: 3 });
    if (path.includes("/mcp-servers")) return jsonOk({ data: installed });
    return jsonOk({});
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  wireFetch();
});
afterEach(cleanup);

describe("ConnectorsPage", () => {
  it("lists the catalog", async () => {
    render(<ConnectorsPage />);
    expect(await screen.findByText("Memory")).toBeInTheDocument();
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("filters by search", async () => {
    render(<ConnectorsPage />);
    await screen.findByText("Memory");
    fireEvent.change(screen.getByLabelText("Buscar conectores"), { target: { value: "github" } });
    await waitFor(() => expect(screen.queryByText("Memory")).not.toBeInTheDocument());
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("marks an already-installed connector", async () => {
    wireFetch([{ name: "memory" }]);
    render(<ConnectorsPage />);
    await waitFor(() => expect(screen.getByText("instalado")).toBeInTheDocument());
  });

  it("installs a credential-free connector in one click", async () => {
    render(<ConnectorsPage />);
    await screen.findByText("Memory");
    fireEvent.click(screen.getByTestId("install-memory"));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
      const body = JSON.parse((post![1] as RequestInit).body as string);
      expect(body).toMatchObject({ name: "memory", transport: "stdio", command: "npx" });
      expect(body.env).toBeUndefined();
    });
  });

  it("asks for the credential before installing a connector that needs one", async () => {
    render(<ConnectorsPage />);
    await screen.findByText("GitHub");

    // First click reveals the field instead of installing blind.
    fireEvent.click(screen.getByTestId("install-github"));
    const field = await screen.findByLabelText("Token do GitHub");
    expect(
      fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST"),
    ).toBe(false);

    fireEvent.change(field, { target: { value: "ghp_secreto" } });
    fireEvent.click(screen.getByTestId("install-github"));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([p, init]) =>
          (init as RequestInit | undefined)?.method === "POST" && !String(p).endsWith("/test"),
      );
      expect(post).toBeTruthy();
      const body = JSON.parse((post![1] as RequestInit).body as string);
      expect(body.env).toEqual({ GITHUB_PERSONAL_ACCESS_TOKEN: "ghp_secreto" });
    });
  });

  it("tests the connection right after installing", async () => {
    render(<ConnectorsPage />);
    await screen.findByText("Memory");
    fireEvent.click(screen.getByTestId("install-memory"));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([p]) => String(p).endsWith("/test"))).toBe(true),
    );
    expect(await screen.findByText(/conectado — 3 tools/)).toBeInTheDocument();
  });
});
