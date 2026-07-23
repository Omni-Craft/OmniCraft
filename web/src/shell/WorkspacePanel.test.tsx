import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { WorkspacePanel } from "./WorkspacePanel";

// The rail's content children are exercised by their own suites; stub them so
// these tests focus on WorkspacePanel's own logic (the open-file tab strip, the
// content branch, and the icon-rail menu).
vi.mock("./FileViewer", () => ({
  FileViewer: ({ path }: { path: string }) => <div data-testid="file-viewer-stub">{path}</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel-stub" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  InlineTerminalsSection: () => <div data-testid="terminals-stub" />,
}));
vi.mock("./SubagentsPanel", () => ({
  SubagentsPanel: () => <div data-testid="subagents-stub" />,
}));
vi.mock("./TodoPanel", () => ({
  TodoPanel: () => <div data-testid="todos-stub" />,
}));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: ({ conversationId }: { conversationId: string }) => (
    <div data-testid="browser-pane-stub">{conversationId}</div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/**
 * Render WorkspacePanel with a complete prop set, overridable per test. The
 * component now uses React Query (rename/archive), so a client is provided.
 */
function renderWorkspace(
  overrides: {
    rightRailTab?: RightRailTab;
    selectedFilePath?: string | null;
    openFiles?: string[];
    showBrowserTab?: boolean;
    showSimulatorTab?: boolean;
    showShellsTab?: boolean;
  } = {},
) {
  const openFileViewer = vi.fn();
  const onCloseFile = vi.fn();
  const onRightRailTabChange = vi.fn();
  const onClose = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <WorkspacePanel
        conversationId="conv_ws"
        width={360}
        handleProps={{ tabIndex: 0 }}
        rightRailTab={overrides.rightRailTab ?? "files"}
        onRightRailTabChange={onRightRailTabChange}
        onClose={onClose}
        showFilesPanel
        showBrowserTab={overrides.showBrowserTab ?? false}
        showSimulatorTab={overrides.showSimulatorTab ?? false}
        changedCount={0}
        showShellsTab={overrides.showShellsTab ?? false}
        terminalsLength={0}
        subagentsWorking={0}
        agentCount={1}
        isClaudeNative={false}
        todosCompleted={0}
        todosTotal={0}
        rootSessionId={null}
        selectedFilePath={overrides.selectedFilePath ?? null}
        openFiles={overrides.openFiles ?? []}
        openFileViewer={openFileViewer}
        onCloseFile={onCloseFile}
        onShowScopeView={vi.fn()}
        onCommentsOpenChange={vi.fn()}
        openTerminalsPanel={vi.fn()}
        permissionLevel={null}
        filesPanelSort={"recent" as ChangedSort}
        onSortChange={vi.fn()}
        filesPanelFlatView={false}
        onFlatViewChange={vi.fn()}
        filesPanelShowHidden={false}
        onShowHiddenChange={vi.fn()}
      />
    </QueryClientProvider>,
  );
  return { openFileViewer, onCloseFile, onRightRailTabChange, onClose };
}

/**
 * Open the rail menu and return its content element. The ⋮ trigger controls
 * the menu's ``open`` state; jsdom doesn't dispatch the pointer events Radix
 * opens on, so drive the sequence explicitly.
 */
function openRailMenu(): HTMLElement {
  const trigger = screen.getByRole("button", { name: "Menu do painel" });
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false });
  fireEvent.click(trigger);
  return screen.getByRole("menu");
}

describe("WorkspacePanel header", () => {
  it("titles the panel with the active panel's name", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });
    expect(screen.getByText("Arquivos")).toBeInTheDocument();
  });

  it("titles the panel with the open file's basename when one is active", () => {
    renderWorkspace({ openFiles: ["docs/README.md"], selectedFilePath: "docs/README.md" });
    // The title is the basename, not the full path.
    expect(screen.getAllByText("README.md").length).toBeGreaterThan(0);
  });

  it("closes the panel via the header ✕", () => {
    const { onClose } = renderWorkspace();
    fireEvent.click(screen.getByRole("button", { name: "Fechar painel" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe("WorkspacePanel icon-rail menu", () => {
  it("lists the available panels and marks the active one", () => {
    renderWorkspace();
    const menu = within(openRailMenu());
    expect(menu.getByRole("menuitem", { name: /arquivos/i })).toBeInTheDocument();
    expect(menu.getByRole("menuitem", { name: /agentes/i })).toBeInTheDocument();
  });

  it("switches panel when a menu item is chosen", () => {
    const { onRightRailTabChange } = renderWorkspace();
    const menu = within(openRailMenu());
    fireEvent.click(menu.getByRole("menuitem", { name: /agentes/i }));
    expect(onRightRailTabChange).toHaveBeenCalledWith("subagents");
  });

  it("lists the Navegador panel only when the browser tab is available", () => {
    renderWorkspace({ showBrowserTab: true });
    expect(
      within(openRailMenu()).getByRole("menuitem", { name: /navegador/i }),
    ).toBeInTheDocument();
  });

  it("omits the Navegador panel when the browser tab is not available", () => {
    renderWorkspace({ showBrowserTab: false });
    expect(within(openRailMenu()).queryByRole("menuitem", { name: /navegador/i })).toBeNull();
  });

  it("offers the session actions", () => {
    renderWorkspace();
    const menu = within(openRailMenu());
    expect(menu.getByRole("menuitem", { name: /renomear/i })).toBeInTheDocument();
    expect(menu.getByRole("menuitem", { name: /arquivar/i })).toBeInTheDocument();
  });
});

describe("WorkspacePanel open-file tabs", () => {
  it("renders a tab per open file labeled by basename", () => {
    renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"] });
    expect(screen.getByText("App.tsx")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
  });

  it("renders no file tabs when none are open", () => {
    renderWorkspace({ openFiles: [] });
    expect(screen.queryByRole("button", { name: /^Fechar [^p]/ })).toBeNull();
  });

  it("marks the active file tab", () => {
    renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
      selectedFilePath: "docs/README.md",
    });
    const readmeTab = screen
      .getByRole("button", { name: "Fechar README.md" })
      .closest("[role='button']");
    const appTab = screen
      .getByRole("button", { name: "Fechar App.tsx" })
      .closest("[role='button']");
    expect(readmeTab).toHaveAttribute("aria-current", "true");
    expect(appTab).toHaveAttribute("aria-current", "false");
  });

  it("activates a file via openFileViewer when its tab body is clicked", () => {
    const { openFileViewer } = renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"] });
    fireEvent.click(screen.getByText("README.md"));
    expect(openFileViewer).toHaveBeenCalledWith("docs/README.md");
  });

  it("closes a file via onCloseFile (and does not also open it) when the x is clicked", () => {
    const { openFileViewer, onCloseFile } = renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Fechar App.tsx" }));
    expect(onCloseFile).toHaveBeenCalledWith("src/App.tsx");
    expect(openFileViewer).not.toHaveBeenCalled();
  });
});

describe("WorkspacePanel content area", () => {
  it("renders the FileViewer for the active path (not the scope panel)", () => {
    renderWorkspace({ openFiles: ["src/App.tsx"], selectedFilePath: "src/App.tsx" });
    expect(screen.getByTestId("file-viewer-stub")).toHaveTextContent("src/App.tsx");
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });

  it("renders the FilesPanel scope view when no file is active on the Files tab", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });
    expect(screen.getByTestId("files-panel-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
  });

  it("mounts the browser pane when the browser tab is selected", () => {
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    expect(screen.getByTestId("browser-pane-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });
});
