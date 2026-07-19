// Tests for the git/PR status bar above the composer.
//
// The bar is contextual, so most of these pin its visibility table: it shows
// up only when the workspace has changes, commits ahead, or a PR — and stays
// out of the way for a clean tree, a session without a workspace, or a git
// failure. The rest cover collapse/expand and the "Criar PR" → "Ver PR"
// swap once a PR is open.
//
// `authenticatedFetch` is stubbed at the seam so the real hook (query key,
// enabled gate, response normalization) runs against canned bodies.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  GitWorkspaceStatusWidget,
  compareUrl,
  isGitWorkspaceStatusVisible,
} from "./GitWorkspaceStatusWidget";
import type { GitPrStatus } from "@/hooks/useGitPrStatus";

const { authenticatedFetchMock } = vi.hoisted(() => ({ authenticatedFetchMock: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/identity")>()),
  authenticatedFetch: (...args: unknown[]) => authenticatedFetchMock(...args),
}));

function status(overrides: Partial<GitPrStatus> = {}): GitPrStatus {
  return {
    object: "session.git_status",
    session_id: "sess_1",
    workspace: "/work/repo",
    branch: "feature/login",
    base_branch: "origin/main",
    ahead: 0,
    behind: 0,
    diff: { added: 0, removed: 0, files: 0 },
    prs: [],
    error: null,
    ...overrides,
  };
}

function pr(overrides: Partial<GitPrStatus["prs"][number]> = {}) {
  return {
    number: 42,
    title: "Add login",
    state: "open" as const,
    ci_status: "success" as const,
    url: "https://github.com/acme/omni/pull/42",
    ...overrides,
  };
}

/**
 * Render the widget with `body` as the endpoint's answer, resolved.
 *
 * Waiting for the query cache to hold the body is what makes the "bar is
 * absent" assertions meaningful — otherwise they'd pass on a bar that just
 * hadn't loaded yet.
 */
async function renderWidget(body: GitPrStatus) {
  authenticatedFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => body });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <GitWorkspaceStatusWidget sessionId="sess_1" />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(client.getQueryData(["session-git-status", "sess_1"])).toBeTruthy());
}

afterEach(() => {
  cleanup();
  authenticatedFetchMock.mockReset();
});

describe("visibility", () => {
  it("shows when the working tree has changes", async () => {
    await renderWidget(status({ diff: { added: 12, removed: 3, files: 2 } }));
    expect(await screen.findByTestId("git-workspace-status")).toBeTruthy();
    expect(screen.getByTestId("git-status-branch").textContent).toBe("feature/login");
    expect(screen.getByTestId("git-status-diff").textContent).toContain("+12");
    expect(screen.getByTestId("git-status-diff").textContent).toContain("-3");
  });

  it("shows when the branch is ahead with no diff", async () => {
    await renderWidget(status({ ahead: 2 }));
    expect(await screen.findByTestId("git-workspace-status")).toBeTruthy();
  });

  it("shows when a PR exists on a clean tree", async () => {
    await renderWidget(status({ prs: [pr()] }));
    expect(await screen.findByTestId("git-workspace-status")).toBeTruthy();
  });

  it("stays out when the tree is clean and there is no PR", async () => {
    await renderWidget(status());
    await waitFor(() => expect(screen.queryByTestId("git-workspace-status")).toBeNull());
  });

  it("stays out when the session has no workspace", async () => {
    await renderWidget(
      status({
        workspace: null,
        branch: null,
        base_branch: null,
        ahead: null,
        behind: null,
        diff: null,
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("git-workspace-status")).toBeNull());
  });

  it("stays out when git failed, even with changes", async () => {
    await renderWidget(
      status({ error: "git timed out", diff: { added: 9, removed: 1, files: 1 } }),
    );
    await waitFor(() => expect(screen.queryByTestId("git-workspace-status")).toBeNull());
  });
});

describe("collapse / expand", () => {
  it("reveals the PR list only once expanded", async () => {
    await renderWidget(status({ diff: { added: 1, removed: 0, files: 1 }, prs: [pr()] }));
    const trigger = await screen.findByRole("button", { name: "Detalhes do workspace" });
    expect(screen.queryByTestId("git-status-pr-42")).toBeNull();

    fireEvent.click(trigger);
    expect(await screen.findByTestId("git-status-pr-42")).toBeTruthy();
    expect(screen.getByTestId("git-status-base").textContent).toContain("origin/main");

    fireEvent.click(trigger);
    await waitFor(() => expect(screen.queryByTestId("git-status-pr-42")).toBeNull());
  });
});

describe("PR action", () => {
  it("links to the open PR instead of offering to create one", async () => {
    await renderWidget(status({ ahead: 1, prs: [pr()] }));
    const link = await screen.findByTestId("git-status-pr-link");
    expect(link.getAttribute("href")).toBe("https://github.com/acme/omni/pull/42");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });

  it("offers a compare link when only a closed PR gives the repo slug", async () => {
    await renderWidget(status({ ahead: 1, prs: [pr({ state: "closed", ci_status: null })] }));
    const link = await screen.findByTestId("git-status-create-pr");
    expect(link.getAttribute("href")).toBe(
      "https://github.com/acme/omni/compare/main...feature%2Flogin?expand=1",
    );
  });

  it("renders no create button when the repo slug can't be derived", async () => {
    await renderWidget(status({ ahead: 1 }));
    await screen.findByTestId("git-workspace-status");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });
});

describe("helpers", () => {
  it("treats a missing status as not visible", () => {
    expect(isGitWorkspaceStatusVisible(undefined)).toBe(false);
    expect(isGitWorkspaceStatusVisible(null)).toBe(false);
  });

  it("has no compare URL without a base branch", () => {
    expect(compareUrl(status({ base_branch: null, prs: [pr()] }))).toBeNull();
  });
});
