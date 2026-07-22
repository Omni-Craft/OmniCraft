import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockUseRepoUpdate } = vi.hoisted(() => ({
  mockUseRepoUpdate: vi.fn(),
}));

vi.mock("./useRepoUpdate", () => ({
  useRepoUpdate: mockUseRepoUpdate,
}));

import { RepoUpdateBanner } from "./RepoUpdateBanner";

function mockStatus(update_available: boolean): void {
  mockUseRepoUpdate.mockReturnValue({
    running_commit: update_available ? "abc123" : "abc123",
    current_commit: update_available ? "def456" : "abc123",
    update_available,
  });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("RepoUpdateBanner", () => {
  it("renders nothing when the checkout has not moved", () => {
    mockStatus(false);
    const { container } = render(<RepoUpdateBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it("tells the user to restart when an update is available", () => {
    mockStatus(true);
    render(<RepoUpdateBanner />);
    expect(screen.getByText(/reinicie o OmniCraft/i)).toBeInTheDocument();
  });

  it("offers no reload button, because a page reload would run stale code", () => {
    // The action is a server restart; a Recarregar button here would lie —
    // reloading the page keeps the same server process on the old commit.
    mockStatus(true);
    render(<RepoUpdateBanner />);
    expect(screen.queryByRole("button", { name: /recarregar/i })).not.toBeInTheDocument();
  });

  it("stays dismissed once dismissed, even while the update persists", () => {
    // The update is still there (poll keeps returning true), but the user
    // said not now — it must not reappear on the next render.
    mockStatus(true);
    render(<RepoUpdateBanner />);
    fireEvent.click(screen.getByRole("button", { name: /dispensar/i }));
    expect(screen.queryByText(/reinicie o OmniCraft/i)).not.toBeInTheDocument();
  });
});
