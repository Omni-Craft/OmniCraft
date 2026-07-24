import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScheduledAgentsPage } from "./ScheduledAgentsPage";
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

// Empty job list + a couple of agents, no hosts — the default preview state.
function mockBackend() {
  fetchMock.mockImplementation((url) => {
    const u = String(url);
    if (u.startsWith("/v1/scheduled-agents") && u === "/v1/scheduled-agents") {
      return Promise.resolve({
        ok: true,
        json: async () => ({ data: [], agents: ["chat", "atlas"] }),
      } as Response);
    }
    if (u === "/v1/hosts") {
      return Promise.resolve({ ok: true, json: async () => ({ hosts: [] }) } as Response);
    }
    // POST create / others
    return Promise.resolve({ ok: true, json: async () => ({}) } as Response);
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  mockBackend();
});
afterEach(cleanup);

describe("ScheduledAgentsPage", () => {
  it("renders the form and the empty state", async () => {
    render(<ScheduledAgentsPage />);
    expect(await screen.findByText("Novo agendamento")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Criar agendamento" })).toBeInTheDocument();
    expect(await screen.findByText("Nenhum agendamento ainda")).toBeInTheDocument();
  });

  it("shows a live next-fire hint in interval mode", async () => {
    render(<ScheduledAgentsPage />);
    await screen.findByText("Novo agendamento");
    // Interval is the default mode.
    expect(screen.getByText(/próximo disparo/)).toBeInTheDocument();
    expect(screen.getByText(/hoje às|amanhã às/)).toBeInTheDocument();
  });

  it("switches schedule panes when a tab is clicked", async () => {
    render(<ScheduledAgentsPage />);
    await screen.findByText("Novo agendamento");
    fireEvent.click(screen.getByRole("button", { name: /Só webhook/ }));
    expect(screen.getByText(/dispara só quando o webhook é chamado/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Horário \(cron\)/ }));
    expect(screen.getByText("Dias úteis 9h")).toBeInTheDocument();
  });

  it("applies a template: fills the name and jumps to cron", async () => {
    render(<ScheduledAgentsPage />);
    await screen.findByText("Novo agendamento");
    fireEvent.click(screen.getByTestId("schedule-template-Resumo diário"));
    expect(screen.getByDisplayValue("Resumo diário")).toBeInTheDocument();
    // Template forces cron mode with its expression.
    expect(screen.getByDisplayValue("0 9 * * 1-5")).toBeInTheDocument();
  });

  it("validates before posting and then creates the schedule", async () => {
    render(<ScheduledAgentsPage />);
    await screen.findByText("Novo agendamento");
    // Missing prompt → validation error, no POST.
    fireEvent.click(screen.getByRole("button", { name: "Criar agendamento" }));
    expect(await screen.findByText("O prompt é obrigatório.")).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText(/O que o agente deve fazer/), {
      target: { value: "Resuma o dia" },
    });
    fireEvent.change(screen.getByPlaceholderText("/Users/voce/projeto"), {
      target: { value: "/tmp/proj" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Criar agendamento" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/scheduled-agents",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });
});
