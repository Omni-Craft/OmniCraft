// Regression test: bulk actions (archive/delete) must operate on the full
// loaded conversation set, not just the currently-VISIBLE (rendered/
// expanded) ones. Before the fix, `BulkActionBar` derived its working set
// from `getVisibleConversationsRef.current()`, which returns `[]` for any
// collapsed section. Selecting rows and then collapsing their section left
// those ids in `selectedIds` but invisible to the bar's `allConversations` —
// so `ownedSelected`/`nonArchivedSelected` silently dropped them, disabling
// bulk delete and hiding the bulk-archive button entirely even though the
// count badge still showed them selected.
//
// `collapsedSections` is local state on `ConversationList`, so collapsing a
// section alone does not re-render `Sidebar` (and thus doesn't refresh the
// `allConversations` prop it hands to `BulkActionBar`). Each test toggles an
// unrelated pinned row's selection after collapsing to force that re-render
// — the same way any subsequent Sidebar-level state change would in the app.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { PINNED_CONVERSATION_IDS_STORAGE_KEY } from "@/shell/sidebarNav";

const mocks = vi.hoisted(() => ({
  bulkArchive: { mutate: vi.fn(), isPending: false, isError: false },
  bulkDelete: { mutate: vi.fn(), isPending: false, isError: false },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => ({ mutate: vi.fn() }),
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => mocks.bulkArchive,
  useBulkDeleteConversations: () => mocks.bulkDelete,
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useMoveToProject: () => ({ mutate: vi.fn() }),
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

// All owned (permission_level null) and not archived — eligible for both
// bulk archive and bulk delete.
const CONV_PIN: Conversation = {
  id: "conv_pin",
  object: "conversation",
  title: "Pinned Session",
  created_at: 1_699_999_999,
  updated_at: 1_699_999_999,
  labels: { "omnicraft.wrapper": "claude-code-native-ui" },
  permission_level: null,
  status: "idle",
};

const CONV_A: Conversation = {
  id: "conv_a",
  object: "conversation",
  title: "Session A",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: { "omnicraft.wrapper": "claude-code-native-ui" },
  permission_level: null,
  status: "idle",
};

const CONV_B: Conversation = {
  id: "conv_b",
  object: "conversation",
  title: "Session B",
  created_at: 1_700_000_001,
  updated_at: 1_700_000_001,
  labels: { "omnicraft.wrapper": "claude-code-native-ui" },
  permission_level: null,
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const withData = {
    data: {
      pages: [
        {
          data: conversations,
          first_id: conversations[0]?.id ?? null,
          last_id: conversations.at(-1)?.id ?? null,
          has_more: false,
        },
      ],
      pageParams: [undefined],
    },
    isLoading: false,
    isError: false,
    error: null,
    fetchNextPage: vi.fn(),
    hasNextPage: false,
    isFetchingNextPage: false,
  } as unknown as ReturnType<typeof useConversations>;
  useConvMock.mockImplementation(() => withData);
}

function renderSidebar() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={["/code"]}>
          <Sidebar open={true} onClose={vi.fn()} />
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function enterSelectionModeAndSelectAll() {
  fireEvent.click(screen.getByRole("button", { name: "Selecionar sessões" }));
  fireEvent.click(screen.getByRole("link", { name: /Pinned Session/ }));
  fireEvent.click(screen.getByRole("link", { name: /Session A/ }));
  fireEvent.click(screen.getByRole("link", { name: /Session B/ }));
}

/** Collapse the "Sessões" section conv_a/conv_b live in (conv_pin stays put
 *  under the always-expanded-by-default "Pinned" section). */
function collapseSessionsSection() {
  fireEvent.click(screen.getByRole("button", { name: "Sessões" }));
}

/**
 * Deselect the still-visible pinned row. `collapsedSections` is local to
 * `ConversationList`, so the collapse click above never re-renders
 * `Sidebar` on its own — this toggle changes Sidebar-level `selectedIds`,
 * forcing the re-render that refreshes `BulkActionBar`'s props against the
 * now-collapsed section, exactly as any later user action would.
 */
function deselectPinned() {
  fireEvent.click(screen.getByRole("link", { name: /Pinned Session/ }));
}

beforeEach(() => {
  // Collapsed-section and pinned-ids state persist to localStorage (see
  // `writeCollapsedSidebarSections` / `writePinnedConversationIds` in
  // Sidebar.tsx), so state left by one test would otherwise leak into the
  // next.
  localStorage.clear();
  localStorage.setItem(PINNED_CONVERSATION_IDS_STORAGE_KEY, JSON.stringify(["conv_pin"]));
  mocks.bulkArchive.mutate.mockReset();
  mocks.bulkDelete.mutate.mockReset();
  mockConversations([CONV_PIN, CONV_A, CONV_B]);
});

afterEach(() => {
  cleanup();
});

describe("bulk actions operate on the full loaded set", () => {
  it("keeps bulk delete enabled (with the full selected count) after the section collapses", () => {
    renderSidebar();
    enterSelectionModeAndSelectAll();

    // Sanity: all three selected while everything is expanded.
    expect(screen.getByTestId("bulk-delete")).toHaveTextContent("Excluir 3");

    collapseSessionsSection();
    deselectPinned();

    // Regression: collapsing "Sessões" must not silently drop conv_a/conv_b
    // from the bulk-action working set just because their section collapsed.
    const deleteBtn = screen.getByTestId("bulk-delete");
    expect(deleteBtn).not.toBeDisabled();
    expect(deleteBtn).toHaveTextContent("Excluir 2");
  });

  it("still offers bulk archive, and archives every selected id, after the section collapses", () => {
    renderSidebar();
    enterSelectionModeAndSelectAll();
    collapseSessionsSection();
    deselectPinned();

    const archiveBtn = screen.getByTestId("bulk-archive");
    fireEvent.click(archiveBtn);

    expect(mocks.bulkArchive.mutate).toHaveBeenCalledTimes(1);
    const [payload] = mocks.bulkArchive.mutate.mock.calls[0];
    expect(payload.archived).toBe(true);
    expect(payload.ids).toHaveLength(2);
    expect(payload.ids).toEqual(expect.arrayContaining(["conv_a", "conv_b"]));
  });
});

describe("select-all toggle is scoped to visible selections", () => {
  it("does not mark 'select all' when a collapsed selection merely matches the visible count", () => {
    renderSidebar();
    fireEvent.click(screen.getByRole("button", { name: "Selecionar sessões" }));

    // Select only Session A (lives in the collapsible "Sessões" section).
    fireEvent.click(screen.getByRole("link", { name: /Session A/ }));

    // Collapse "Sessões": conv_a leaves the visible set, so the only visible row
    // is the pinned one. The total selection (1) now equals the visible count
    // (1) — the exact shape that used to flip the toggle to "all selected" even
    // though the selected row is off-screen and the visible pinned row is not.
    collapseSessionsSection();

    expect(screen.getByRole("button", { name: "Selecionar tudo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Desmarcar tudo" })).toBeNull();
  });

  it("marks 'select all' once every visible row is selected", () => {
    renderSidebar();
    // Selects the pinned row plus Session A/B — every visible row.
    enterSelectionModeAndSelectAll();

    expect(screen.getByRole("button", { name: "Desmarcar tudo" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Selecionar tudo" })).toBeNull();
  });
});
