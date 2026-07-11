import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { SessionStateBadge } from "./SessionStateBadge";
import type { SessionState } from "@/hooks/useSessionState";

function renderBadge(state: SessionState) {
  return render(
    <TooltipProvider>
      <SessionStateBadge state={state} />
    </TooltipProvider>,
  );
}

afterEach(cleanup);

describe("SessionStateBadge — per-state rendering", () => {
  it("renders awaiting as a compact amber marker with a count-aware accessible label", () => {
    renderBadge({ kind: "awaiting", count: 3 });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "awaiting");
    expect(badge).toHaveAttribute("aria-label", "3 prompts de aprovação aguardando");
    // The approval indicator is a compact amber bell (so it never crowds the
    // row title), but keeps an accessible "Aguardando resposta" label that
    // screen readers and the collapsed-project header surface.
    expect(badge).toHaveTextContent("Aguardando resposta");
  });

  it("uses singular wording when only one prompt is pending", () => {
    renderBadge({ kind: "awaiting", count: 1 });
    expect(screen.getByTestId("session-state-badge")).toHaveAttribute(
      "aria-label",
      "1 prompt de aprovação aguardando",
    );
  });

  it("renders running with a spinning grey spinner", () => {
    const { container } = renderBadge({ kind: "running" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("data-state", "running");
    // The running indicator is a grey spinner; a missing spinner
    // (or the old success-tone dot grid) means it regressed.
    const spinner = container.querySelector('[data-testid="running-dot"]');
    expect(spinner).not.toBeNull();
    expect(spinner?.getAttribute("class")).toContain("animate-spin");
    expect(spinner?.getAttribute("class")).toContain("text-muted-foreground");
    expect(container.querySelector(".bg-success")).toBeNull();
  });

  it("renders unseen messages as a solid (non-pulsing) brand-pink dot", () => {
    const { container } = renderBadge({ kind: "unseen" });
    const badge = screen.getByTestId("session-state-badge");
    expect(badge).toHaveAttribute("aria-label", "Novas mensagens");
    expect(badge).toHaveAttribute("data-state", "unseen");
    // Unread reuses the brand-pink token but stays static; the pulsing
    // variant (running-pulse-dot) is reserved for the running state.
    const dot = container.querySelector(".bg-brand-accent");
    expect(dot).not.toBeNull();
    expect(dot?.getAttribute("class")).not.toContain("running-pulse-dot");
    expect(container.querySelector(".bg-info")).toBeNull();
  });
});
