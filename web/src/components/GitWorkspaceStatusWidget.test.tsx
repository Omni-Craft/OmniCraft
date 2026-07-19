// Tests for the git/PR status bar above the composer.
//
// The bar is contextual, so most of these pin its visibility table: it shows
// up only when the workspace has changes, commits ahead, or a PR — and stays
// out of the way for a clean tree, a session without a workspace, or a git
// failure. The rest cover collapse/expand and the "Criar PR" → "Ver PR"
// swap once a PR is open — including the two-click confirmation that opens
// it, which must never POST on the first click nor twice on the second.
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
    repo_slug: "acme/omni",
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
async function renderWidget(body: GitPrStatus, sessionId = "sess_1") {
  authenticatedFetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => body });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <GitWorkspaceStatusWidget sessionId={sessionId} />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(client.getQueryData(["session-git-status", sessionId])).toBeTruthy());
  return { client, view };
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
  it("offers a create button for a dirty branch with no PR", async () => {
    await renderWidget(status({ diff: { added: 5, removed: 1, files: 2 } }));
    const button = await screen.findByTestId("git-status-create-pr");
    expect(button.textContent).toBe("Criar PR");
    expect(button.getAttribute("aria-label")).toContain("pede confirmação");
  });

  it("swaps the create button for the PR link once a PR shows up", async () => {
    const { client } = await renderWidget(status({ ahead: 1 }));
    expect(await screen.findByTestId("git-status-create-pr")).toBeTruthy();
    expect(screen.queryByTestId("git-status-pr-link")).toBeNull();

    // Next poll: someone opened the PR for this branch.
    client.setQueryData(["session-git-status", "sess_1"], status({ ahead: 1, prs: [pr()] }));

    const link = await screen.findByTestId("git-status-pr-link");
    expect(link.getAttribute("href")).toBe("https://github.com/acme/omni/pull/42");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });

  it("renders no create button without a repo slug", async () => {
    await renderWidget(status({ ahead: 1, repo_slug: null }));
    await screen.findByTestId("git-workspace-status");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });

  it("renders no create button for a slug that isn't a plain owner/repo", async () => {
    // A slug carrying a query string would send the button somewhere else.
    await renderWidget(status({ ahead: 1, repo_slug: "acme/omni?x=1" }));
    await screen.findByTestId("git-workspace-status");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });

  it("keeps offering the compare link while only closed PRs exist", async () => {
    await renderWidget(status({ ahead: 1, prs: [pr({ state: "closed", ci_status: null })] }));
    expect(await screen.findByTestId("git-status-create-pr")).toBeTruthy();
    expect(screen.queryByTestId("git-status-pr-link")).toBeNull();
  });
});

describe("creating the pull request", () => {
  /**
   * Render with the git-status GET answering `body` and the pull-request POST
   * answering `post`, so the click path runs against the real hook.
   */
  async function renderCreatable(post: () => unknown, body: GitPrStatus = status({ ahead: 1 })) {
    authenticatedFetchMock.mockImplementation(async (url: unknown) =>
      String(url).endsWith("/pull-request")
        ? post()
        : { ok: true, status: 200, json: async () => body },
    );
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <GitWorkspaceStatusWidget sessionId="sess_1" />
      </QueryClientProvider>,
    );
    return {
      button: await screen.findByTestId("git-status-create-pr"),
      posts: () =>
        authenticatedFetchMock.mock.calls.filter((call) =>
          String(call[0]).endsWith("/pull-request"),
        ),
    };
  }

  const created = (overrides = {}) => ({
    ok: true,
    status: 200,
    json: async () => ({
      object: "session.pull_request",
      session_id: "sess_1",
      number: 7,
      url: "https://github.com/acme/omni/pull/7",
      created: true,
      title: "Add login",
      ...overrides,
    }),
  });

  const refused = (httpStatus: number, code: string, message: string) => ({
    ok: false,
    status: httpStatus,
    json: async () => ({ error: { code, message } }),
  });

  it("arms on the first click without opening anything", async () => {
    const { button, posts } = await renderCreatable(created);
    fireEvent.click(button);

    await waitFor(() => expect(button.textContent).toBe("Confirmar?"));
    expect(button.getAttribute("aria-label")).toContain("abrir um pull request de feature/login");
    expect(posts()).toHaveLength(0);
  });

  it("opens the pull request on the second click and shows it", async () => {
    const { button, posts } = await renderCreatable(created);
    fireEvent.click(button);
    fireEvent.click(button);

    const link = await screen.findByTestId("git-status-pr-link");
    expect(link.textContent).toBe("Ver PR #7");
    expect(link.getAttribute("href")).toBe("https://github.com/acme/omni/pull/7");
    expect(posts()).toHaveLength(1);
    expect((posts()[0][1] as { method?: string }).method).toBe("POST");
    expect(screen.queryByTestId("git-status-create-pr")).toBeNull();
  });

  it("sends a single POST however fast the button is clicked", async () => {
    let release: (value: unknown) => void = () => {};
    const { button, posts } = await renderCreatable(
      () => new Promise((resolve) => (release = resolve)),
    );
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(button.getAttribute("aria-label")).toBe("Criando PR…"));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(button.textContent).toContain("Criando PR…");

    fireEvent.click(button);
    fireEvent.click(button);
    expect(posts()).toHaveLength(1);

    release(created());
    expect((await screen.findByTestId("git-status-pr-link")).textContent).toBe("Ver PR #7");
  });

  it("treats an already-open pull request as a success", async () => {
    const { button } = await renderCreatable(() => created({ created: false, number: 12 }));
    fireEvent.click(button);
    fireEvent.click(button);

    expect((await screen.findByTestId("git-status-pr-link")).textContent).toBe("Ver PR #12");
    expect(screen.queryByTestId("git-status-pr-error")).toBeNull();
  });

  // Every refusal arrives with a message already written for the user; the
  // bar prints it and leaves the compare page as the way through.
  it.each([
    [400, "invalid_input", "GitHub is not configured for this workspace."],
    [403, "forbidden", "The GitHub token cannot write pull requests."],
    [409, "conflict", "Push the branch before opening a pull request."],
  ])("reports a %s refusal and keeps the compare fallback", async (code, slug, message) => {
    const { button } = await renderCreatable(() => refused(code, slug, message));
    fireEvent.click(button);
    fireEvent.click(button);

    const error = await screen.findByTestId("git-status-pr-error");
    expect(error.textContent).toContain(message);
    expect(screen.getByTestId("git-status-compare-link").getAttribute("href")).toBe(
      "https://github.com/acme/omni/compare/main...feature%2Flogin?expand=1",
    );
    expect(screen.getByTestId("git-status-compare-link").getAttribute("rel")).toBe(
      "noopener noreferrer",
    );
    expect(screen.getByTestId("git-status-create-pr").textContent).toBe("Criar PR");
  });

  it("disarms on Escape", async () => {
    const { button, posts } = await renderCreatable(created);
    fireEvent.click(button);
    await waitFor(() => expect(button.textContent).toBe("Confirmar?"));

    fireEvent.keyDown(button, { key: "Escape" });
    await waitFor(() => expect(button.textContent).toBe("Criar PR"));
    // The next click has to arm again rather than fire.
    fireEvent.click(button);
    expect(posts()).toHaveLength(0);
  });

  it("disarms when the button loses focus", async () => {
    const { button, posts } = await renderCreatable(created);
    fireEvent.click(button);
    await waitFor(() => expect(button.textContent).toBe("Confirmar?"));

    fireEvent.blur(button);
    await waitFor(() => expect(button.textContent).toBe("Criar PR"));
    fireEvent.click(button);
    expect(posts()).toHaveLength(0);
  });
});

describe("CI state", () => {
  it("names each state in text, not just color", async () => {
    await renderWidget(
      status({
        prs: [
          pr({ number: 1, ci_status: "success" }),
          pr({ number: 2, ci_status: "failure" }),
          pr({ number: 3, ci_status: "pending" }),
        ],
      }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Detalhes do workspace" }));
    expect(await screen.findByTestId("git-status-pr-1")).toBeTruthy();
    expect(screen.getByTestId("git-status-pr-1").textContent).toContain("CI passou");
    expect(screen.getByTestId("git-status-pr-2").textContent).toContain("CI falhou");
    expect(screen.getByTestId("git-status-pr-3").textContent).toContain("CI rodando");
  });

  it("shows nothing for an unknown CI state — null is not a failure", async () => {
    await renderWidget(status({ prs: [pr({ ci_status: null })] }));
    fireEvent.click(await screen.findByRole("button", { name: "Detalhes do workspace" }));
    const row = await screen.findByTestId("git-status-pr-42");
    expect(row.textContent).not.toContain("CI");
    expect(screen.queryByTestId("ci-failure")).toBeNull();
  });
});

describe("diff summary", () => {
  it("names the files when the change set has no line counts", async () => {
    await renderWidget(status({ diff: { added: 0, removed: 0, files: 3 } }));
    expect((await screen.findByTestId("git-status-diff")).textContent).toBe("3 arquivo(s)");
  });
});

describe("session switch", () => {
  it("does not carry the previous session's status into the next one", async () => {
    authenticatedFetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => status({ diff: { added: 4, removed: 2, files: 1 } }),
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = render(
      <QueryClientProvider client={client}>
        <GitWorkspaceStatusWidget sessionId="sess_1" />
      </QueryClientProvider>,
    );
    expect(await screen.findByTestId("git-status-branch")).toBeTruthy();

    // Switching sessions must not answer with the old workspace: the bar
    // would name the wrong branch, and ChatPage would hide the composer's.
    let resolveNext: (value: unknown) => void = () => {};
    authenticatedFetchMock.mockReturnValue(
      new Promise((resolve) => {
        resolveNext = resolve;
      }),
    );
    rerender(
      <QueryClientProvider client={client}>
        <GitWorkspaceStatusWidget sessionId="sess_2" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(screen.queryByTestId("git-workspace-status")).toBeNull());

    resolveNext({
      ok: true,
      status: 200,
      json: async () => status({ session_id: "sess_2", branch: "main", ahead: 3 }),
    });
    await waitFor(() => expect(screen.getByTestId("git-status-branch").textContent).toBe("main"));
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

describe("repo slug validation", () => {
  it("builds the compare URL from a plain owner/repo", () => {
    expect(compareUrl(status({ repo_slug: "acme-co/omni.craft_1" }))).toBe(
      "https://github.com/acme-co/omni.craft_1/compare/main...feature%2Flogin?expand=1",
    );
  });

  // The slug is free-form text off a git remote, spliced into a URL: anything
  // that could bend the path or the destination must drop the button instead.
  it.each([
    ["query string", "owner/repo?x=1"],
    ["fragment", "owner/repo#f"],
    ["path traversal", "../../evil"],
    ["traversal segment", "owner/.."],
    ["three segments", "a/b/c"],
    ["one segment", "owner"],
    ["empty", ""],
    ["empty owner", "/repo"],
    ["empty repo", "owner/"],
    ["absolute url", "https://evil.example.com/owner/repo"],
    ["backslash", "owner\\repo"],
    ["space", "owner/re po"],
    ["at sign", "owner@host/repo"],
  ])("rejects a slug with a %s", (_label, slug) => {
    expect(compareUrl(status({ repo_slug: slug }))).toBeNull();
  });
});
