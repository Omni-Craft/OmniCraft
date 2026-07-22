import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SimulatorPane } from "./SimulatorPane";

const hostState = { fetcher: null as null | ((path: string) => Promise<Response>) };
const shellState = { electron: true };

vi.mock("@/lib/host", () => ({
  hostFetch: (path: string) =>
    hostState.fetcher ? hostState.fetcher(path) : Promise.reject(new Error("no fetcher")),
}));

vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => shellState.electron,
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (sel: (s: { status: string }) => unknown) => sel({ status: "idle" }),
}));

beforeEach(() => {
  hostState.fetcher = null;
  shellState.electron = true;
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SimulatorPane", () => {
  it("shows a desktop-only hint outside the Electron shell", async () => {
    shellState.electron = false;
    render(<SimulatorPane conversationId="conv_1" />);
    expect(await screen.findByText(/máquina com Xcode/i)).toBeInTheDocument();
  });

  it("shows the empty state when no simulator is booted (409)", async () => {
    hostState.fetcher = (path: string) => {
      if (path.endsWith("/devices")) {
        return Promise.resolve(new Response(JSON.stringify({ ok: true, booted: null })));
      }
      // screenshot → 409
      return Promise.resolve(new Response(JSON.stringify({ ok: false }), { status: 409 }));
    };
    render(<SimulatorPane conversationId="conv_1" />);
    await waitFor(() =>
      expect(screen.getByText(/Nenhum simulador em execução/i)).toBeInTheDocument(),
    );
  });

  it("renders the control bar", () => {
    render(<SimulatorPane conversationId="conv_1" onClose={() => {}} />);
    expect(screen.getByLabelText("Recarregar")).toBeInTheDocument();
    expect(screen.getByLabelText("Fechar")).toBeInTheDocument();
  });
});
