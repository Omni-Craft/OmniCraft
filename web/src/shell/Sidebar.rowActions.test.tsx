// Tests for the sidebar conversation-row quick actions:
//   1. A desktop quick pin/unpin button (`quick-pin-conversation`) and a
//      mobile-only kebab Pin item (`pin-conversation`) — two affordances for
//      the same pin toggle, split by viewport (responsive Tailwind classes).
//   2. Double-clicking a row to enter inline rename (ConversationRow's
//      `onDoubleClick`), gated on edit permission.
// See ConversationRow / ConversationEditRow in Sidebar.tsx.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";

// Controllable rename mutation so the double-click test can assert the
// committed title was forwarded to the PATCH. Declared via vi.hoisted so the
// vi.mock factory (hoisted above imports) can reference it.
const mocks = vi.hoisted(() => ({
  rename: { mutate: vi.fn() },
  moveToProject: { mutate: vi.fn() },
}));

vi.mock("@/hooks/useConversations", () => ({
  useConversations: vi.fn(),
  useConnectedConversations: () => [],
  useStopAndDeleteConversation: () => ({
    mutate: vi.fn(),
    reset: vi.fn(),
    isPending: false,
    isError: false,
    variables: undefined,
  }),
  usePinnedConversationBackfill: () => [],
  useRenameConversation: () => mocks.rename,
  useArchiveConversation: () => ({ mutate: vi.fn() }),
  useBulkArchiveConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkDeleteConversations: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useBulkStopSessions: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  useStopSession: () => ({ mutate: vi.fn() }),
  useProjects: () => ({ data: [] }),
  useMoveToProject: () => mocks.moveToProject,
  useDeleteProject: () => ({ mutate: vi.fn(), isPending: false, isError: false }),
  fetchProjectSessionIds: () => Promise.resolve([]),
  PROJECT_LABEL_KEY: "omni_project",
}));

// Heavy sibling widgets pull their own hooks/providers; stub them so this
// test stays scoped to the conversation row.
vi.mock("./AgentTypeFilter", () => ({ AgentTypeFilter: () => null }));
vi.mock("./ReportIssueButton", () => ({ ReportIssueButton: () => null }));
vi.mock("@/components/PermissionsModal", () => ({ PermissionsModal: () => null }));
// Force a multi-user (non-local) server so the "Shared with me" tab renders —
// jsdom's default loopback origin would otherwise read as single-user and hide
// the tabs the shared-session row actions rely on.
vi.mock("@/lib/serverOrigin", () => ({ isCurrentServerLocal: () => false }));

import { type Conversation, useConversations } from "@/hooks/useConversations";
import { __resetReadStateForTests, seedReadState } from "@/hooks/useUnseenConversations";
import { Sidebar } from "./Sidebar";

const useConvMock = vi.mocked(useConversations);

const CONV: Conversation = {
  id: "conv_1",
  object: "conversation",
  title: "My Session",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_000,
  labels: {},
  permission_level: null, // owner → can edit + pin
  status: "idle",
};

function mockConversations(conversations: Conversation[]) {
  const dataResult = {
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
  useConvMock.mockImplementation(() => dataResult);
}

// `activeId` mounts the sidebar at `/c/:conversationId` (via a matching
// Route so `useParams` populates), making that row the active one — the
// rest of the suite renders at `/` where no row is active.
function renderSidebar(activeId?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const sidebar = <Sidebar open={true} onClose={vi.fn()} />;
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[activeId ? `/c/${activeId}` : "/code"]}>
          {activeId ? (
            <Routes>
              <Route path="/c/:conversationId" element={sidebar} />
            </Routes>
          ) : (
            sidebar
          )}
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.rename.mutate.mockReset();
  mocks.moveToProject.mutate.mockReset();
  useConvMock.mockReset();
  // The read-state mirror is module-level (in-memory), so reset it between
  // tests to avoid a mark-unread leaking into later rows.
  __resetReadStateForTests();
  mockConversations([CONV]);
});

afterEach(cleanup);

describe("quick pin/unpin hover button", () => {
  it("toggles the pin without opening the kebab menu, moving the row under Pinned", () => {
    renderSidebar();

    // No "Fixadas" section to start; the row lives under Recent.
    expect(screen.queryByText("Fixadas")).toBeNull();
    const pinButton = screen.getByTestId("quick-pin-conversation");
    expect(pinButton).toHaveAttribute("aria-label", "Fixar conversa");

    fireEvent.click(pinButton);

    // The row is now grouped under a "Fixadas" header, and the quick button
    // flips to its unpin affordance — both prove the toggle ran through the
    // sidebar's pin state (not just a local no-op).
    const pinnedHeader = screen.getByText("Fixadas");
    const pinnedSection = pinnedHeader.closest("section")!;
    expect(within(pinnedSection).getByText("My Session")).toBeInTheDocument();
    expect(screen.getByTestId("quick-pin-conversation")).toHaveAttribute(
      "aria-label",
      "Desafixar conversa",
    );

    // Persisted to localStorage so the pin survives a reload (same contract
    // as the kebab's Pin item).
    expect(localStorage.getItem("omnicraft:pinned-conversation-ids")).toContain("conv_1");

    // Clicking again unpins: the Fixadas section disappears.
    fireEvent.click(screen.getByTestId("quick-pin-conversation"));
    expect(screen.queryByText("Fixadas")).toBeNull();
  });

  it("also offers Pin in the kebab menu (mobile affordance) and toggles the same pin state", () => {
    renderSidebar();

    expect(screen.queryByText("Fixadas")).toBeNull();

    // Radix DropdownMenu opens on pointerdown, not click.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });

    // The kebab carries a Pin item (mobile-only via `md:hidden`, but always in
    // the DOM since jsdom doesn't evaluate media queries). Clicking it drives
    // the same pin state as the quick button — the row moves under "Fixadas".
    const pinItem = screen.getByTestId("pin-conversation");
    expect(pinItem).toHaveTextContent("Fixar");
    fireEvent.click(pinItem);

    const pinnedHeader = screen.getByText("Fixadas");
    const pinnedSection = pinnedHeader.closest("section")!;
    expect(within(pinnedSection).getByText("My Session")).toBeInTheDocument();
    expect(localStorage.getItem("omnicraft:pinned-conversation-ids")).toContain("conv_1");
  });

  it("splits the two pin affordances by viewport via Tailwind responsive classes", () => {
    // jsdom doesn't evaluate CSS media queries, so both affordances live in the
    // DOM regardless of viewport — the mobile/desktop split is purely the
    // responsive classes. Assert those classes directly: the kebab Pin item is
    // hidden from `md` up (desktop), and the quick button is hidden below `md`
    // (mobile) but shown from `md` up. Together they guarantee exactly one pin
    // affordance is visible at any breakpoint.
    renderSidebar();

    // Desktop quick button: hidden on mobile, revealed from `md` up. The reveal
    // uses `md:inline-flex` (not `md:block`) so the button stays a flex
    // container — see the centering regression test below.
    const quickButton = screen.getByTestId("quick-pin-conversation");
    expect(quickButton).toHaveClass("hidden", "md:inline-flex");

    // Kebab Pin item: present in the menu but hidden from `md` up, so it only
    // surfaces on mobile.
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.getByTestId("pin-conversation")).toHaveClass("md:hidden");
  });

  it("reveals the quick-pin button without breaking icon centering", () => {
    // The Button base centers its icon with `inline-flex` + `items-center
    // justify-center`. The desktop reveal MUST keep a flex display: revealing
    // it with `md:block` overrode `inline-flex`, made the
    // centering classes inert, and shoved the pin glyph to the button's
    // top-left corner (~6px off-center). Guard the display so the reveal
    // stays flex and the glyph stays centered.
    renderSidebar();

    const quickButton = screen.getByTestId("quick-pin-conversation");
    // The centering classes are present...
    expect(quickButton).toHaveClass("items-center", "justify-center");
    // ...and the desktop reveal makes the button a flex container (so those
    // classes actually take effect), rather than a block (which would not).
    expect(quickButton).toHaveClass("md:inline-flex");
    expect(quickButton).not.toHaveClass("md:block");
  });
});

describe("double-click to rename", () => {
  it("enters inline rename on double-click and commits the new title on Enter", () => {
    renderSidebar();

    // No edit field until the row is double-clicked.
    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();

    const row = screen.getByRole("link", { name: /My Session/ });
    fireEvent.dblClick(row);

    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "Renamed Session" } });
    fireEvent.keyDown(input, { key: "Enter" });

    // The committed (trimmed) title is forwarded to the rename mutation with
    // the row's id — proving the double-click path drives the same rename as
    // the kebab's Rename item.
    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_1", title: "Renamed Session" });
  });

  it("does not commit the rename on an IME composition Enter", () => {
    renderSidebar();

    fireEvent.dblClick(screen.getByRole("link", { name: /My Session/ }));
    const input = screen.getByTestId("rename-conversation-input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "半分の名前" } });

    // Enter with keyCode 229 is the IME "confirm candidates" keystroke, not a
    // submit — the half-composed title must stay in the field.
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
    expect(screen.getByTestId("rename-conversation-input")).toBeInTheDocument();

    // A plain Enter once composition ends still commits.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.rename.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.rename.mutate).toHaveBeenCalledWith({ id: "conv_1", title: "半分の名前" });
  });

  it("does not enter rename on double-click for a viewer-only row", () => {
    // permission_level 1 is below the edit threshold (>= 2), so the kebab's
    // Rename item is disabled and double-click must be inert too. A viewer-only
    // (non-owner) session lives on the "Shared with me" tab, so switch to it
    // before reaching for the row.
    mockConversations([{ ...CONV, permission_level: 1 }]);
    renderSidebar();
    // Radix Tabs triggers activate on mousedown (primary button), not click.
    fireEvent.mouseDown(screen.getByTestId("sidebar-tab-shared"), { button: 0 });

    fireEvent.dblClick(screen.getByRole("link", { name: /My Session/ }));

    expect(screen.queryByTestId("rename-conversation-input")).toBeNull();
    expect(mocks.rename.mutate).not.toHaveBeenCalled();
  });
});

describe("create-new-project input (IME guard)", () => {
  // Reach the inline "Nome do projeto…" field: kebab → "Move to project"
  // submenu → "Criar novo projeto". Committing it files the row via
  // moveToProject.mutate, so that mock stands in for the commit.
  function openNewProjectInput() {
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("move-to-project"));
    fireEvent.click(screen.getByText("Criar novo projeto"));
    return screen.getByPlaceholderText("Nome do projeto…") as HTMLInputElement;
  }

  it("does not create a project on an IME composition Enter, but a plain Enter commits", () => {
    renderSidebar();
    const input = openNewProjectInput();
    fireEvent.change(input, { target: { value: "新規プロジェクト" } });

    // keyCode 229 is the IME "confirm candidates" keystroke, not a submit.
    fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
    expect(mocks.moveToProject.mutate).not.toHaveBeenCalled();

    // A plain Enter once composition ends still files the row into the project.
    fireEvent.keyDown(input, { key: "Enter" });
    expect(mocks.moveToProject.mutate).toHaveBeenCalledTimes(1);
    expect(mocks.moveToProject.mutate).toHaveBeenCalledWith({
      id: "conv_1",
      project: "新規プロジェクト",
    });
  });
});

describe("mark as unread", () => {
  it("re-lights the row's unread dot via an explicit mark-unread", () => {
    renderSidebar();

    // The row starts seen (no baseline) — no unread marker.
    expect(screen.queryByText("(não lida)")).toBeNull();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    // The dot's accessible label appears immediately (in-tab tick on the
    // optimistic mirror write); the baseline is also synced to the server.
    expect(screen.getByText("(não lida)")).toBeInTheDocument();
  });

  it("holds the dot on a running session until the turn finishes", () => {
    mockConversations([{ ...CONV, status: "running" }]);
    renderSidebar();

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    // The dot stays suppressed mid-turn (the explicit override lifts the
    // active-row suppression, not the running one).
    expect(screen.queryByText("(não lida)")).toBeNull();

    // Once the turn finishes (row re-renders as idle), the dot lights — the
    // baseline (kept in the in-memory mirror) now reads unseen for a
    // finished session.
    cleanup();
    mockConversations([{ ...CONV, status: "idle" }]);
    renderSidebar();
    expect(screen.getByText("(não lida)")).toBeInTheDocument();
  });

  it("lights the dot on the active thread you're currently viewing", () => {
    // The active row normally suppresses the dot (you're reading it), but an
    // explicit mark-unread is a deliberate flag, so the dot must show.
    renderSidebar("conv_1");

    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    fireEvent.click(screen.getByTestId("mark-unread-conversation"));

    expect(screen.getByText("(não lida)")).toBeInTheDocument();
  });

  it("is hidden once the row is already unread", () => {
    // Seed a baseline below updated_at (as the conversation list would) so
    // the row is already unseen.
    seedReadState([{ id: "conv_1", viewer_last_seen: CONV.updated_at - 1 }]);
    renderSidebar();

    expect(screen.getByText("(não lida)")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByTestId("conversation-actions"), { button: 0 });
    expect(screen.queryByTestId("mark-unread-conversation")).toBeNull();
  });
});

describe("right-click context menu", () => {
  it("opens the same action items as the kebab and drives the same handlers", () => {
    renderSidebar();

    // Nothing in the DOM until the row is right-clicked (the kebab menu is
    // closed, so its items aren't rendered either).
    expect(screen.queryByTestId("rename-conversation")).toBeNull();

    fireEvent.contextMenu(screen.getByRole("link", { name: /My Session/ }));

    // The context menu carries the full set of kebab actions — same testids,
    // so it renders from the shared ConversationMenuItems body.
    expect(screen.getByTestId("share-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("rename-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("move-to-project")).toBeInTheDocument();
    expect(screen.getByTestId("archive-conversation")).toBeInTheDocument();
    expect(screen.getByTestId("delete-conversation")).toBeInTheDocument();

    // Selecting Rename runs the same path as the kebab / double-click: the
    // inline rename input appears.
    fireEvent.click(screen.getByTestId("rename-conversation"));
    expect(screen.getByTestId("rename-conversation-input")).toBeInTheDocument();
  });
});
