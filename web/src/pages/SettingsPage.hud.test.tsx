// Tests for Settings → Desktop → HUD flutuante (`/settings/hud`).
//
// The section's whole job is to not lie about a setting that lives in the
// desktop shell, one IPC hop away. So alongside the three visibility modes,
// most of what's asserted here is the UNKNOWN states: a shell that never
// answered, a settings.json that wouldn't parse, a write that failed — each
// has to read "desconhecido", never a switch resting at off. And in a browser
// tab, where there is no HUD at all, the section says so instead of offering a
// control that would do nothing.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { HudNotificationSettings, HudSettingsRead } from "@/lib/nativeBridge";

const mocks = vi.hoisted(() => ({
  isElectronShell: true,
  getHudSettings: vi.fn(),
  setHudSettings: vi.fn(),
}));

vi.mock("@/lib/nativeBridge", () => ({
  isElectronShell: () => mocks.isElectronShell,
  getHudSettings: mocks.getHudSettings,
  setHudSettings: mocks.setHudSettings,
  getCliStatus: vi.fn().mockResolvedValue(null),
  resetCliPath: vi.fn().mockResolvedValue(null),
}));
vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: "system", systemTheme: "light", setTheme: vi.fn() }),
}));
vi.mock("@/lib/embedded", () => ({ useIsEmbedded: () => false }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ accounts_enabled: false, login_url: null }),
}));
vi.mock("@/lib/accountsApi", () => ({ logout: vi.fn(), changePassword: vi.fn() }));
vi.mock("@/lib/identity", () => ({
  resolveIdentity: () => Promise.resolve(null),
  getCurrentIsAdmin: () => false,
}));
vi.mock("@/hooks/useConversations", () => ({
  useConversations: () => ({ data: { pages: [] }, isLoading: false }),
  useArchiveConversation: () => ({ mutate: vi.fn(), isPending: false }),
  useStopAndDeleteConversation: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { SettingsPage } from "./SettingsPage";

/** The notification preferences a never-configured install runs on. */
function notifications(overrides: Partial<HudNotificationSettings> = {}): HudNotificationSettings {
  return {
    permission: true,
    budget: true,
    stuck: true,
    completion: true,
    quietFrom: null,
    quietTo: null,
    budgetThreshold: 0.8,
    ...overrides,
  };
}

/** A readable settings answer from the shell. */
function settings(overrides: Partial<HudSettingsRead> = {}): HudSettingsRead {
  return {
    readable: true,
    enabled: true,
    mode: "always",
    notifications: notifications(),
    sound: false,
    ...overrides,
  };
}

function renderHudSection() {
  return render(
    <TooltipProvider>
      <MemoryRouter initialEntries={["/settings/hud"]}>
        <SettingsPage />
      </MemoryRouter>
    </TooltipProvider>,
  );
}

/** Render and wait for the initial probe to settle. */
async function renderSettled() {
  renderHudSection();
  await waitFor(() => expect(screen.queryByText("Verificando…")).not.toBeInTheDocument());
}

beforeEach(() => {
  mocks.isElectronShell = true;
  mocks.getHudSettings.mockReset().mockResolvedValue(settings());
  mocks.setHudSettings.mockReset();
});
afterEach(cleanup);

describe("Settings → HUD flutuante", () => {
  it("reflects the shell's stored on/off state and mode", async () => {
    mocks.getHudSettings.mockResolvedValue(settings({ enabled: true, mode: "hide-when-idle" }));
    await renderSettled();

    expect(screen.getByTestId("hud-enabled")).toHaveAttribute("data-state", "checked");
    expect(screen.getByTestId("hud-mode-hide-when-idle")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("hud-mode-always")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("hud-mode-attention-only")).toHaveAttribute("aria-checked", "false");
  });

  it("persists each of the three visibility modes through the shell", async () => {
    for (const mode of ["always", "hide-when-idle", "attention-only"] as const) {
      cleanup();
      mocks.getHudSettings.mockResolvedValue(settings({ mode: "always" }));
      mocks.setHudSettings.mockReset().mockResolvedValue(settings({ mode }));
      await renderSettled();

      fireEvent.click(screen.getByTestId(`hud-mode-${mode}`));
      expect(mocks.setHudSettings).toHaveBeenCalledWith({ mode });
      await waitFor(() =>
        expect(screen.getByTestId(`hud-mode-${mode}`)).toHaveAttribute("aria-checked", "true"),
      );
    }
  });

  it("turns the HUD off through the shell, not through local state", async () => {
    mocks.getHudSettings.mockResolvedValue(settings({ enabled: true }));
    mocks.setHudSettings.mockResolvedValue(settings({ enabled: false }));
    await renderSettled();

    fireEvent.click(screen.getByTestId("hud-enabled"));
    expect(mocks.setHudSettings).toHaveBeenCalledWith({ enabled: false });
    await waitFor(() =>
      expect(screen.getByTestId("hud-enabled")).toHaveAttribute("data-state", "unchecked"),
    );
  });

  it("says the state is UNKNOWN when the shell never answers — never 'off'", async () => {
    // getHudSettings resolves null for a main process that didn't reply, a
    // shell too old for the bridge, and a call that threw.
    mocks.getHudSettings.mockResolvedValue(null);
    await renderSettled();

    expect(screen.getByTestId("hud-settings-unknown")).toHaveTextContent(/desconhecido/i);
    expect(screen.getByTestId("hud-settings-unknown")).toHaveTextContent(
      /não quer dizer que ele esteja desligado/i,
    );
    // No control may be shown resting at a value we do not have.
    expect(screen.queryByTestId("hud-enabled")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hud-mode-always")).not.toBeInTheDocument();
  });

  it("says the state is UNKNOWN when settings.json could not be read", async () => {
    mocks.getHudSettings.mockResolvedValue({
      readable: false,
      enabled: null,
      mode: null,
      notifications: null,
      sound: null,
    });
    await renderSettled();

    expect(screen.getByTestId("hud-settings-unknown")).toBeInTheDocument();
    expect(screen.queryByTestId("hud-enabled")).not.toBeInTheDocument();
  });

  it("re-probes the shell on retry and shows the settings once they arrive", async () => {
    mocks.getHudSettings
      .mockResolvedValueOnce(null)
      .mockResolvedValue(settings({ mode: "always" }));
    await renderSettled();
    expect(screen.getByTestId("hud-settings-unknown")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Tentar de novo" }));
    await waitFor(() => expect(screen.getByTestId("hud-enabled")).toBeInTheDocument());
    expect(mocks.getHudSettings).toHaveBeenCalledTimes(2);
  });

  it("drops to UNKNOWN when a change could not be saved", async () => {
    // The write never came back, so whether it landed is unknown — leaving the
    // old values on screen would present them as current.
    mocks.getHudSettings.mockResolvedValue(settings({ mode: "always" }));
    mocks.setHudSettings.mockResolvedValue(null);
    await renderSettled();

    fireEvent.click(screen.getByTestId("hud-mode-attention-only"));
    await waitFor(() =>
      expect(screen.getByTestId("hud-settings-unknown")).toHaveTextContent(
        /não pôde ser salva e o estado atual do HUD é desconhecido/i,
      ),
    );
    expect(screen.queryByTestId("hud-mode-attention-only")).not.toBeInTheDocument();
  });

  it("in a browser tab, says the HUD is a desktop-app window instead of offering a toggle", async () => {
    mocks.isElectronShell = false;
    renderHudSection();

    expect(await screen.findByTestId("hud-settings-browser")).toHaveTextContent(
      /app desktop do OmniCraft/i,
    );
    expect(screen.queryByTestId("hud-enabled")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hud-mode-always")).not.toBeInTheDocument();
    expect(screen.queryByTestId("hud-settings-unknown")).not.toBeInTheDocument();
    // Nothing to ask: there is no shell on the other end of the bridge.
    expect(mocks.getHudSettings).not.toHaveBeenCalled();
  });

  describe("notificações", () => {
    it("says outright that nothing fires with the HUD turned off", async () => {
      // The trap this text exists for: four switches sitting at "on" read as
      // "I will be told", and with the HUD off nobody is watching the feed.
      mocks.getHudSettings.mockResolvedValue(settings({ enabled: false }));
      await renderSettled();

      expect(screen.getByTestId("hud-notifications-scope")).toHaveTextContent(
        /só funcionam com o HUD ligado/i,
      );
      expect(screen.getByTestId("hud-notifications-scope")).toHaveTextContent(
        /escondido.*continua observando/i,
      );
      expect(screen.getByTestId("hud-notifications-hud-off")).toBeInTheDocument();
    });

    it("reflects each stored category and sends ONE of them when toggled", async () => {
      mocks.getHudSettings.mockResolvedValue(
        settings({ notifications: notifications({ budget: false }) }),
      );
      mocks.setHudSettings.mockResolvedValue(settings());
      await renderSettled();

      expect(screen.getByTestId("hud-notify-budget")).toHaveAttribute("data-state", "unchecked");
      expect(screen.getByTestId("hud-notify-permission")).toHaveAttribute("data-state", "checked");

      fireEvent.click(screen.getByTestId("hud-notify-stuck"));
      // The patch names only what changed; the shell merges it into the rest,
      // so the page must not send a whole sub-object built from its own state.
      expect(mocks.setHudSettings).toHaveBeenCalledWith({ notifications: { stuck: false } });
      await waitFor(() =>
        expect(screen.getByTestId("hud-notify-budget")).toHaveAttribute("data-state", "checked"),
      );
    });

    it("toggles the app-wide sound, not a HUD-only copy of it", async () => {
      mocks.getHudSettings.mockResolvedValue(settings({ sound: false }));
      mocks.setHudSettings.mockResolvedValue(settings({ sound: true }));
      await renderSettled();

      fireEvent.click(screen.getByTestId("hud-notify-sound"));
      expect(mocks.setHudSettings).toHaveBeenCalledWith({ sound: true });
      await waitFor(() =>
        expect(screen.getByTestId("hud-notify-sound")).toHaveAttribute("data-state", "checked"),
      );
    });

    it("turns quiet hours on with a range, and clears BOTH ends when turned off", async () => {
      mocks.getHudSettings.mockResolvedValue(settings());
      mocks.setHudSettings.mockResolvedValue(
        settings({ notifications: notifications({ quietFrom: "22:00", quietTo: "07:00" }) }),
      );
      await renderSettled();

      expect(screen.queryByTestId("hud-quiet-from")).not.toBeInTheDocument();
      fireEvent.click(screen.getByTestId("hud-quiet-enabled"));
      expect(mocks.setHudSettings).toHaveBeenCalledWith({
        notifications: { quietFrom: "22:00", quietTo: "07:00" },
      });

      await waitFor(() => expect(screen.getByTestId("hud-quiet-from")).toBeInTheDocument());
      expect(screen.getByTestId("hud-quiet-to")).toHaveValue("07:00");

      // Half a range is not a span the shell can silence on, so both go.
      fireEvent.click(screen.getByTestId("hud-quiet-enabled"));
      expect(mocks.setHudSettings).toHaveBeenLastCalledWith({
        notifications: { quietFrom: null, quietTo: null },
      });
      await waitFor(() => expect(screen.getByTestId("hud-quiet-from")).toBeInTheDocument());
    });

    it("sends both ends when one is edited, and ignores a half-typed time", async () => {
      mocks.getHudSettings.mockResolvedValue(
        settings({ notifications: notifications({ quietFrom: "22:00", quietTo: "07:00" }) }),
      );
      mocks.setHudSettings.mockResolvedValue(settings());
      await renderSettled();

      fireEvent.change(screen.getByTestId("hud-quiet-to"), { target: { value: "" } });
      expect(mocks.setHudSettings).not.toHaveBeenCalled();

      fireEvent.change(screen.getByTestId("hud-quiet-to"), { target: { value: "08:30" } });
      expect(mocks.setHudSettings).toHaveBeenCalledWith({
        notifications: { quietFrom: "22:00", quietTo: "08:30" },
      });
      await waitFor(() => expect(screen.queryByTestId("hud-quiet-from")).not.toBeInTheDocument());
    });

    it("shows the stored budget threshold", async () => {
      mocks.getHudSettings.mockResolvedValue(
        settings({ notifications: notifications({ budgetThreshold: 0.5 }) }),
      );
      await renderSettled();

      expect(screen.getByTestId("hud-budget-threshold")).toHaveTextContent("50%");
    });

    it("says UNKNOWN — never off — when only the notifications failed to read", async () => {
      // A settings.json whose hud blob parsed but whose notification block did
      // not. Four switches resting at "off" would be the page inventing it.
      mocks.getHudSettings.mockResolvedValue(
        settings({ readable: false, enabled: null, mode: null, notifications: null, sound: null }),
      );
      await renderSettled();

      expect(screen.getByTestId("hud-settings-unknown")).toHaveTextContent(/desconhecido/i);
      expect(screen.queryByTestId("hud-notify-permission")).not.toBeInTheDocument();
      expect(screen.queryByTestId("hud-quiet-enabled")).not.toBeInTheDocument();
    });
  });
});
