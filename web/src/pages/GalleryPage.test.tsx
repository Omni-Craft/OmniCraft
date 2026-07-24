import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GalleryPage } from "./GalleryPage";
import { authenticatedFetch } from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  authenticatedFetch: vi.fn(),
}));
vi.mock("@/lib/routing", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/routing")>()),
  useNavigate: () => vi.fn(),
}));

const fetchMock = vi.mocked(authenticatedFetch);

const AGENTS = [
  {
    id: "atlas",
    name: "atlas",
    description: "Orquestrador de planejamento e arquitetura.",
    category: "orquestrador",
    harness: "claude",
    subagents: 2,
    subagent_names: ["reviewer", "surveyor"],
    skills: ["breakdown-tasks", "design-doc", "risk-map"],
    prompt_preview: "",
    installed: false,
  },
  {
    id: "chat",
    name: "chat",
    description: "A conversa geral, sem filesystem.",
    category: "conversa",
    harness: null,
    subagents: 0,
    subagent_names: [],
    skills: [],
    prompt_preview: "",
    installed: true,
  },
  {
    id: "fabrica-completa",
    name: "fabrica-completa",
    description: "A fábrica completa.",
    category: "fábrica",
    harness: null,
    subagents: 2,
    subagent_names: ["backend", "web"],
    skills: ["a", "b", "c", "d"],
    prompt_preview: "",
    installed: false,
  },
];

function mockList(agents = AGENTS) {
  fetchMock.mockImplementation((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      return Promise.resolve({ ok: true, json: async () => ({}) } as Response);
    }
    return Promise.resolve({ ok: true, json: async () => ({ data: agents }) } as Response);
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  mockList();
});
afterEach(cleanup);

describe("GalleryPage", () => {
  it("renders a card per agent with its category subtitle", async () => {
    render(<GalleryPage />);
    expect(await screen.findByText("atlas")).toBeInTheDocument();
    expect(screen.getByText("chat")).toBeInTheDocument();
    expect(screen.getByText("fabrica-completa")).toBeInTheDocument();
    // Publisher · category subtitle.
    expect(screen.getByText("omnicraft · orquestrador")).toBeInTheDocument();
  });

  it("shows Todos and Instalados counts and a tab per category", async () => {
    render(<GalleryPage />);
    const todos = await screen.findByRole("button", { name: /Todos/ });
    expect(todos).toHaveTextContent("3");
    expect(screen.getByRole("button", { name: /Instalados/ })).toHaveTextContent("1");
    expect(screen.getByRole("button", { name: "Orquestradores" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Fábricas" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Conversa" })).toBeInTheDocument();
  });

  it("filters to installed agents when the Instalados tab is clicked", async () => {
    render(<GalleryPage />);
    await screen.findByText("atlas");
    fireEvent.click(screen.getByRole("button", { name: /Instalados/ }));
    expect(screen.getByText("chat")).toBeInTheDocument();
    expect(screen.queryByText("atlas")).not.toBeInTheDocument();
  });

  it("filters by the search box", async () => {
    render(<GalleryPage />);
    await screen.findByText("atlas");
    fireEvent.change(screen.getByPlaceholderText("Buscar agentes"), {
      target: { value: "fabrica" },
    });
    expect(screen.getByText("fabrica-completa")).toBeInTheDocument();
    expect(screen.queryByText("atlas")).not.toBeInTheDocument();
    expect(screen.queryByText("chat")).not.toBeInTheDocument();
  });

  it("opens a details dialog listing all sub-agents and skills", async () => {
    render(<GalleryPage />);
    await screen.findByText("atlas");
    // The first Detalhes button belongs to the atlas card.
    fireEvent.click(screen.getAllByRole("button", { name: "Detalhes" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Sub-agentes/)).toBeInTheDocument();
    expect(within(dialog).getByText("surveyor")).toBeInTheDocument();
    expect(within(dialog).getByText("risk-map")).toBeInTheDocument();
  });

  it("posts to the install endpoint when Instalar is clicked", async () => {
    render(<GalleryPage />);
    await screen.findByText("atlas");
    // atlas is the first uninstalled card, so its Instalar comes first.
    fireEvent.click(screen.getAllByRole("button", { name: "Instalar" })[0]);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/gallery/agents/atlas/install",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("tolerates a backend response missing the category field", async () => {
    mockList([{ ...AGENTS[0], category: undefined } as never]);
    render(<GalleryPage />);
    expect(await screen.findByText("atlas")).toBeInTheDocument();
    // The catch-all tab appears instead of crashing on the missing category.
    expect(screen.getByRole("button", { name: "Outros" })).toBeInTheDocument();
  });
});
