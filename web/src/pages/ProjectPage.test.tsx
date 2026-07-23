import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectPage } from "./ProjectPage";

const fetchMock = vi.fn();

vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (path: string, init?: RequestInit) => fetchMock(path, init),
}));

vi.mock("@/lib/routing", () => ({
  useParams: () => ({ name: "Acme" }),
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));

vi.mock("@/hooks/useConversations", () => ({
  useProjectSessions: () => ({
    data: { pages: [{ data: [{ id: "conv_1", title: "Primeira sessão" }] }] },
  }),
}));

const DOCS = [
  {
    id: "pdoc_1",
    filename: "contrato.pdf",
    bytes: 2048,
    content_type: "application/pdf",
    text_chars: 900,
    searchable: true,
    created_at: 1,
  },
  {
    id: "pdoc_2",
    filename: "diagrama.png",
    bytes: 4096,
    content_type: "image/png",
    text_chars: 0,
    searchable: false,
    created_at: 2,
  },
];

function jsonOk(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
}

function wireFetch(docs = DOCS) {
  fetchMock.mockImplementation((path: string, init?: RequestInit) => {
    if (init?.method === "POST" || init?.method === "DELETE") return jsonOk({});
    if (path.endsWith("/documents")) return jsonOk({ data: docs });
    return jsonOk({});
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  wireFetch();
  vi.stubGlobal("confirm", () => true);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ProjectPage", () => {
  it("shows the project's documents", async () => {
    render(<ProjectPage />);
    expect(await screen.findByText("contrato.pdf")).toBeInTheDocument();
    expect(screen.getByText("diagrama.png")).toBeInTheDocument();
  });

  it("distinguishes searchable documents from stored-only ones", async () => {
    render(<ProjectPage />);
    await screen.findByText("contrato.pdf");
    // A document with no extractable text is still on the shelf, but the UI
    // must not imply the agent can find it by content.
    expect(screen.getByText("pesquisável")).toBeInTheDocument();
    expect(screen.getByText("sem texto")).toBeInTheDocument();
  });

  it("shows an empty state before anything is uploaded", async () => {
    wireFetch([]);
    render(<ProjectPage />);
    expect(await screen.findByText(/Nenhum documento ainda/)).toBeInTheDocument();
  });

  it("uploads a picked file to the project", async () => {
    render(<ProjectPage />);
    await screen.findByText("contrato.pdf");

    const input = screen.getByLabelText("Adicionar documento");
    const file = new File(["conteudo"], "notas.md", { type: "text/markdown" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
      expect(String(post![0])).toContain("/v1/projects/Acme/documents");
      expect((post![1] as RequestInit).body).toBeInstanceOf(FormData);
    });
  });

  it("deletes a document after confirming", async () => {
    render(<ProjectPage />);
    await screen.findByText("contrato.pdf");
    fireEvent.click(screen.getByLabelText("Remover contrato.pdf"));

    await waitFor(() => {
      const del = fetchMock.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "DELETE",
      );
      expect(del).toBeTruthy();
      expect(String(del![0])).toContain("pdoc_1");
    });
  });

  it("lists the project's sessions", async () => {
    render(<ProjectPage />);
    expect(await screen.findByText("Primeira sessão")).toBeInTheDocument();
  });
});
