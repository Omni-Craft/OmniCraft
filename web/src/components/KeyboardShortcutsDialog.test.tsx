import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KeyboardShortcutsDialog, openKeyboardShortcuts } from "./KeyboardShortcutsDialog";

// The pinned-session row shows in both shells; only its chord differs (Alt in
// the browser). Default the mock to browser (false); flip per-test for native.
const isNativeShell = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isNativeShell: () => isNativeShell(),
  // DialogContent (rendered here) reads isIOSShell to size modals for the iOS
  // keyboard; this suite exercises the browser path, so it's always false.
  isIOSShell: () => false,
}));

beforeEach(() => {
  isNativeShell.mockReturnValue(false);
});
afterEach(cleanup);

// jsdom's navigator is non-mac, so the modifier glyph renders as "Ctrl".
function toggleViaHotkey() {
  fireEvent.keyDown(window, { key: "/", ctrlKey: true });
}

describe("KeyboardShortcutsDialog", () => {
  it("renders nothing until opened", () => {
    render(<KeyboardShortcutsDialog />);
    expect(screen.queryByText("Enviar mensagem")).toBeNull();
  });

  it("opens on the modifier+/ hotkey and lists one shortcut from each group", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();

    expect(screen.getByText("Atalhos de teclado")).toBeTruthy();
    // General / In chats / Navigation / View / Slash commands — one each.
    expect(screen.getByText("Abrir paleta de comandos")).toBeTruthy();
    expect(screen.getByText("Mostrar atalhos de teclado")).toBeTruthy();
    expect(screen.getByText("Enviar mensagem")).toBeTruthy();
    expect(screen.getByText("Recuperar prompt anterior")).toBeTruthy();
    expect(screen.getByText("Sessão anterior")).toBeTruthy();
    expect(screen.getByText("Alternar barra lateral de conversas")).toBeTruthy();
    expect(screen.getByText("Navegar sugestões")).toBeTruthy();
  });

  it("toggles closed on a second hotkey press", async () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    expect(screen.getByText("Enviar mensagem")).toBeTruthy();

    toggleViaHotkey();
    await waitFor(() => expect(screen.queryByText("Enviar mensagem")).toBeNull());
  });

  it("opens when openKeyboardShortcuts() is dispatched (menu entry path)", async () => {
    render(<KeyboardShortcutsDialog />);
    openKeyboardShortcuts();
    // The event dispatch isn't wrapped in act(), so wait for the re-render.
    expect(await screen.findByText("Enviar mensagem")).toBeTruthy();
  });

  it("shows the pinned-session shortcut with the Alt chord in a plain browser", () => {
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    const row = screen.getByText("Ir para sessão fixada (1–10)").closest("li");
    expect(row).toBeTruthy();
    // Browser chord adds Alt (jsdom navigator is non-mac → "Alt") + the 1…0 chip.
    expect(within(row!).getByText("Alt")).toBeTruthy();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });

  it("shows the pinned-session shortcut without Alt in the Electron shell", () => {
    isNativeShell.mockReturnValue(true);
    render(<KeyboardShortcutsDialog />);
    toggleViaHotkey();
    const row = screen.getByText("Ir para sessão fixada (1–10)").closest("li");
    expect(row).toBeTruthy();
    expect(within(row!).queryByText("Alt")).toBeNull();
    expect(within(row!).getByText("1…0")).toBeTruthy();
  });
});
