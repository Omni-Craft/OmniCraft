import {
  ArchiveIcon,
  BotIcon,
  CheckIcon,
  EllipsisVerticalIcon,
  FileIcon,
  GlobeIcon,
  ListTodoIcon,
  PencilIcon,
  SmartphoneIcon,
  SunMoonIcon,
  TerminalIcon,
  XIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useArchiveConversation, useRenameConversation } from "@/hooks/useConversations";
import { BrowserPane } from "@/components/BrowserPane/BrowserPane";
import { SimulatorPane } from "@/components/SimulatorPane/SimulatorPane";
import { FilesPanel } from "./FilesPanel";
import { FileViewer } from "./FileViewer";
import type { ChangedSort } from "./FlatFileList";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import { SubagentsPanel } from "./SubagentsPanel";
import { TodoPanel } from "./TodoPanel";
import { type RightRailTab, TAB_BADGE_BASE } from "./railTabs";

// ---------------------------------------------------------------------------
// FileTabsStrip — open file tabs rendered in the top rail tab strip, as peers
// of the fixed Files/Terminals/Agents/Tasks tabs. Each tab is a cell with the
// file's basename and an "x" close button. Clicking the cell activates the
// tab (opening its viewer); clicking the x closes it. No own scroll container
// or flex-1: the parent strip's overflow-x-auto scrolls the whole row.
// ---------------------------------------------------------------------------

function FileTabsStrip({
  openFiles,
  activeFilePath,
  onFileSelect,
  onCloseFile,
}: {
  /** Ordered list of open file paths. */
  openFiles: string[];
  /** Currently active file path, or null when a scope/other tab is active. */
  activeFilePath: string | null;
  /** Activate a tab by path. */
  onFileSelect: (path: string) => void;
  /** Close a tab by path. */
  onCloseFile: (path: string) => void;
}) {
  // Scroll the active tab into view when it changes (e.g. a newly opened file
  // appended past the visible edge). `inline: "nearest"` scrolls whichever
  // ancestor is the scroller — the outer strip (<500px) or the file-tabs
  // region (≥500px) — without us hard-coding which one.
  const activeTabRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeFilePath]);
  return (
    <div className="flex items-center gap-0.5">
      {openFiles.map((path) => {
        const name = path.split("/").pop() ?? path;
        const active = path === activeFilePath;
        return (
          <div
            key={path}
            ref={active ? activeTabRef : undefined}
            role="button"
            tabIndex={0}
            aria-current={active}
            title={path}
            onClick={() => onFileSelect(path)}
            onAuxClick={(e) => {
              // Middle click (button 1) closes the tab, matching browser /
              // editor tab conventions.
              if (e.button === 1) {
                e.preventDefault();
                onCloseFile(path);
              }
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onFileSelect(path);
              }
            }}
            className={cn(
              // Match the fixed TabsTrigger pill's box metrics (h-32 / px-12 /
              // rounded-8 / 13px medium) so file tabs and Files/Terminals tabs
              // are the same height and the active chip lines up across both
              // sets. `group/tab` drives the hover-revealed close overlay below.
              // `overflow-hidden` clips the hover-close gradient overlay to the
              // pill's rounded corners so its rectangular edges can't poke out.
              "group/tab relative flex h-[32px] min-w-0 max-w-[320px] shrink-0 cursor-pointer items-center justify-center gap-[6px] overflow-hidden rounded-[8px] px-[12px] text-[13px] font-medium leading-5 transition-colors",
              active
                ? "bg-[color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))] text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <FileIcon className="size-4 shrink-0" />
            <span className="min-w-0 truncate">{name}</span>
            {/* Close button: hidden until hover, then revealed over a gradient
                that fades the truncated filename into the tab's background so
                the "x" never collides with the text. The fade color tracks the
                tab's own background — the gray chip when active, card otherwise
                — so the mask blends in instead of flashing a white patch. */}
            <span
              className={cn(
                "absolute inset-y-0 right-[2px] flex items-center pl-[12px] pr-[4px] opacity-0 transition-opacity group-hover/tab:opacity-100",
                active
                  ? "[background:linear-gradient(to_right,transparent,color-mix(in_srgb,var(--muted-foreground)_15%,var(--card))_40%)]"
                  : "[background:linear-gradient(to_right,transparent,var(--card)_40%)]",
              )}
            >
              <button
                type="button"
                aria-label={`Fechar ${name}`}
                className="flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  onCloseFile(path);
                }}
              >
                <XIcon className="size-4" />
              </button>
            </span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Props for {@link WorkspacePanel}. All state lives in AppShell; this
 * component is a pure view. Handlers wrap the AppShell setters so the
 * shell keeps single-source-of-truth over file/terminal/panel state.
 */
interface WorkspacePanelProps {
  /** Active session id — panels read the workspace against it. */
  conversationId: string;
  /** Current rail width (px), driven by the resize handle. */
  width: number;
  /** Whether the panel is closed/collapsed (hides it from keyboard nav + assistive tech). */
  inert?: boolean;
  /**
   * Props for the left-edge resize handle (onMouseDown/onKeyDown + ARIA),
   * from ``useResizableInlinePanel().handleProps``.
   */
  handleProps: React.HTMLAttributes<HTMLDivElement> & { tabIndex: number };
  /** Selected rail tab, e.g. ``"files"``. */
  rightRailTab: RightRailTab;
  /**
   * Switch rail tabs. AppShell owns the side effects (clearing any open
   * file + its comments + URL) so they can't drift from the tab state.
   */
  onRightRailTabChange: (next: RightRailTab) => void;
  /** Collapse the panel (the header's ✕). AppShell owns ``rightPanelOpen``. */
  onClose?: () => void;
  /** Whether the Files tab is available (agent spec exposes an os_env). */
  showFilesPanel: boolean;
  /** Whether the Browser tab is available — Electron shell only (hidden in a
   *  plain web build, which has no embedded WebContentsView). */
  showBrowserTab: boolean;
  /** Whether the iOS Simulator tab is available — desktop shell only, where
   *  the runner Mac's simulator can be reached and previewed. */
  showSimulatorTab: boolean;
  /** Count of changed files, shown as the Files tab badge. */
  changedCount: number;
  /**
   * Whether the Shells tab is available — AppShell's combined gate
   * (not a native wrapper, AND either a shell exists or the agent's
   * spec declares shell access, which makes the tab show by default
   * with its "+ New shell" empty state).
   */
  showShellsTab: boolean;
  /** Number of open shells, shown as the Shells tab badge when > 0. */
  terminalsLength: number;
  /** How many child agents are actively working (Agents tab badge). */
  subagentsWorking: number;
  /**
   * Total agents in the session tree, main agent included (Agents tab
   * badge denominator) — starts at 1 for a lone agent.
   */
  agentCount: number;
  /** Whether this is a claude-native session (gates the Tasks tab). */
  isClaudeNative: boolean;
  /** Number of completed todos (Tasks tab badge numerator). */
  todosCompleted: number;
  /** Total todo count (Tasks tab badge denominator + visibility gate). */
  todosTotal: number;
  /**
   * The "root" session id for the Agents tab — the active session's
   * parent when inside a child, else the active id. May be null while
   * the session snapshot loads.
   */
  rootSessionId: string | null;
  /** Active file path, or null when the Files tab shows a scope view. */
  selectedFilePath: string | null;
  /** Ordered list of open file tabs, shown as a strip in the Files panel. */
  openFiles: string[];
  /** Open a file in the inline viewer (adds/activates its tab). */
  openFileViewer: (path: string) => void;
  /** Close a single open file tab by path. */
  onCloseFile: (path: string) => void;
  /** Deselect the active file tab to reveal the scope view (Changed/All). */
  onShowScopeView: () => void;
  /** Surface the file viewer's comments-open state up to AppShell (it
   *  widens the rail to fit the comments column). */
  onCommentsOpenChange: (open: boolean) => void;
  /** Expand a terminal into the full-width terminals push panel. */
  openTerminalsPanel: (key: string) => void;
  /** Viewer's permission level (gates edit affordances). */
  permissionLevel: number | null;
  /** Changed-files sort order, shared with the viewer's prev/next order. */
  filesPanelSort: ChangedSort;
  /** Change the changed-files sort order. */
  onSortChange: (sort: ChangedSort) => void;
  /** Files view scope: false = full tree, true = changed-only flat list. */
  filesPanelFlatView: boolean;
  /** Toggle the Files view scope (persisted by AppShell). */
  onFlatViewChange: (flat: boolean) => void;
  /** Whether the Files panel shows dotfiles/hidden entries. */
  filesPanelShowHidden: boolean;
  /** Toggle hidden-file visibility in the Files panel. */
  onShowHiddenChange: (show: boolean) => void;
}

/**
 * WorkspacePanel — the desktop right "Workspace" rail, rendered as a
 * floating card (bg-card, rounded, bordered, shadowed) sitting below the
 * full-width chat header band. Internally tabbed between Files,
 * Terminals, Agents and Tasks so each can claim the full rail height
 * instead of competing for a vertically-split slot.
 *
 * Desktop-only (``hidden md:flex``): on mobile the rail's contents are
 * reached via the header's session-menu FAB → full-screen drawers. The
 * card is drag-resizable via a handle on its left edge.
 *
 * Render gating (default-open, hidden while a push panel owns the
 * right side) lives in AppShell — this component assumes it should
 * render when mounted.
 */
export function WorkspacePanel({
  conversationId,
  width,
  handleProps,
  inert,
  rightRailTab,
  onRightRailTabChange,
  onClose,
  showFilesPanel,
  showBrowserTab,
  showSimulatorTab,
  changedCount,
  showShellsTab,
  terminalsLength,
  subagentsWorking,
  agentCount,
  isClaudeNative,
  todosCompleted,
  todosTotal,
  rootSessionId,
  selectedFilePath,
  openFiles,
  openFileViewer,
  onCloseFile,
  onShowScopeView,
  onCommentsOpenChange,
  openTerminalsPanel,
  permissionLevel,
  filesPanelSort,
  onSortChange,
  filesPanelFlatView,
  onFlatViewChange,
  filesPanelShowHidden,
  onShowHiddenChange,
}: WorkspacePanelProps) {
  // Memoized so FileViewer's Escape-to-close effect doesn't re-subscribe its
  // window keydown listener on every render — an inline arrow would change
  // identity each render and thrash the effect's add/remove cycle.
  const handleCloseTab = useCallback(() => {
    if (selectedFilePath !== null) onCloseFile(selectedFilePath);
  }, [onCloseFile, selectedFilePath]);

  const { resolvedTheme, setTheme } = useTheme();
  const rename = useRenameConversation();
  const archive = useArchiveConversation();
  const [menuOpen, setMenuOpen] = useState(false);

  // The panels available for this session, in display order. Both the header
  // title and the ⋮ menu read from this one list, so they can't drift.
  const panels = (
    [
      showFilesPanel && {
        id: "files" as const,
        label: "Arquivos",
        Icon: FileIcon,
        badge: changedCount > 0 ? String(changedCount) : null,
      },
      {
        id: "subagents" as const,
        label: "Agentes",
        Icon: BotIcon,
        badge: subagentsWorking > 0 ? `${subagentsWorking}/${agentCount}` : String(agentCount),
        // Green only while a child is actually working — otherwise the count
        // is a neutral total, matching the old tab-strip badge.
        badgeTint: subagentsWorking > 0 ? "bg-success/15 text-success" : null,
      },
      showShellsTab && {
        id: "terminals" as const,
        label: "Terminais",
        Icon: TerminalIcon,
        badge: terminalsLength > 0 ? String(terminalsLength) : null,
      },
      isClaudeNative &&
        todosTotal > 0 && {
          id: "todos" as const,
          label: "Tarefas",
          Icon: ListTodoIcon,
          badge: `${todosCompleted}/${todosTotal}`,
        },
      showBrowserTab && {
        id: "browser" as const,
        label: "Navegador",
        Icon: GlobeIcon,
        badge: null,
      },
      showSimulatorTab && {
        id: "simulator" as const,
        label: "Simulador de iOS",
        Icon: SmartphoneIcon,
        badge: null,
      },
    ] as const
  ).filter(Boolean) as {
    id: RightRailTab;
    label: string;
    Icon: typeof FileIcon;
    badge: string | null;
    badgeTint?: string | null;
  }[];

  const activePanel = panels.find((p) => p.id === rightRailTab) ?? panels[0];
  // When a file is open its name is the title; otherwise the active panel's.
  const headerTitle =
    selectedFilePath !== null
      ? (selectedFilePath.split("/").pop() ?? "Arquivo")
      : (activePanel?.label ?? "Painel");

  const doRename = () => {
    const next = window.prompt("Novo nome da sessão:");
    if (next && next.trim()) rename.mutate({ id: conversationId, title: next.trim() });
  };
  const doArchive = () => {
    if (window.confirm("Arquivar esta sessão? Ela sai da lista, mas o histórico fica.")) {
      archive.mutate({ id: conversationId, archived: true });
    }
  };

  const railBtn =
    "flex size-9 items-center justify-center rounded-[10px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground";

  return (
    <aside
      aria-label="Workspace"
      inert={inert}
      // Floating card on desktop: detached from the chat + window edges by
      // margins (no left margin — the left edge hosts the resize handle and
      // butts against main), rounded, bordered, and lifted off the
      // bg-sidebar canvas with a shadow — matching the sidebar's card
      // treatment. ``mt-14`` clears the fixed 56px chat header (the header
      // is an absolute overlay); it's tunable alongside the chat's
      // ``pt-20`` clearance. ``z-40`` lifts the card above the header
      // (``z-30``) — the card starts below the button row, so sitting
      // above the header never covers a control.
      // ``@container/rail`` makes the rail a named container-query context so
      // the tab strip can switch scroll behavior on the rail's own width
      // (see the strip below) without a JS width listener.
      className="@container/rail relative z-40 hidden md:flex md:shrink-0 md:flex-row md:overflow-hidden md:mt-14 md:mr-2 md:mb-2 md:rounded-xl md:border md:border-border md:bg-card md:shadow-lg md:min-h-0"
      style={{ width }}
    >
      {/* Left-edge horizontal resize handle. */}
      <div
        {...handleProps}
        className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
      />
      {/* Panel card — title header, the open-file tabs when any, then the
          content. The tab strip that used to switch panels here moved to the
          icon rail on the right; the active panel's name is the title now. */}
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden bg-card">
        <header className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-3">
          <span
            data-testid="workspace-panel-title"
            className="min-w-0 flex-1 truncate text-[13.5px] font-semibold"
          >
            {headerTitle}
          </span>
          {onClose && (
            <button
              type="button"
              aria-label="Fechar painel"
              onClick={onClose}
              className="text-muted-foreground transition-colors hover:text-foreground"
            >
              <XIcon className="size-4" />
            </button>
          )}
        </header>
        {openFiles.length > 0 && (
          <div className="flex shrink-0 items-center overflow-x-auto overflow-y-hidden border-b border-border px-2 py-1.5 [scrollbar-width:thin] [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar]:h-1">
            <FileTabsStrip
              openFiles={openFiles}
              activeFilePath={selectedFilePath}
              onFileSelect={openFileViewer}
              onCloseFile={onCloseFile}
            />
          </div>
        )}
        {/* Tab content — single slot. Files holds FileViewer when a
          file is open, FilesPanel otherwise; Shells holds the
          list-only inline section (clicking a row opens the shell in
          the main view — no in-rail xterm); Subagents lists the
          root's children + a "main" link back to the parent.
          The Shells branch is unreachable when its tab is hidden —
          native wrappers, claude-native sub-agents, or no shell
          attached. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
          {selectedFilePath !== null ? (
            <FileViewer
              frameless
              open
              conversationId={conversationId}
              path={selectedFilePath}
              onClose={onShowScopeView}
              onCloseTab={handleCloseTab}
              onNavigateTo={openFileViewer}
              permissionLevel={permissionLevel}
              onCommentsOpenChange={onCommentsOpenChange}
              sort={filesPanelSort}
            />
          ) : rightRailTab === "browser" && showBrowserTab ? (
            // Embedded browser (Electron only) — BrowserPane self-gates and
            // measures this rail slot to position the native view over it.
            <BrowserPane conversationId={conversationId} className="min-h-0 flex-1" />
          ) : rightRailTab === "simulator" && showSimulatorTab ? (
            <SimulatorPane conversationId={conversationId} className="min-h-0 flex-1" />
          ) : rightRailTab === "subagents" && rootSessionId ? (
            <SubagentsPanel conversationId={conversationId} rootSessionId={rootSessionId} />
          ) : rightRailTab === "todos" && isClaudeNative ? (
            <TodoPanel frameless />
          ) : rightRailTab === "terminals" && showShellsTab ? (
            <InlineTerminalsSection conversationId={conversationId} onExpand={openTerminalsPanel} />
          ) : (
            showFilesPanel && (
              <FilesPanel
                frameless
                onFileSelect={openFileViewer}
                flatView={filesPanelFlatView}
                onFlatViewChange={onFlatViewChange}
                showHidden={filesPanelShowHidden}
                onShowHiddenChange={onShowHiddenChange}
                sort={filesPanelSort}
                onSortChange={onSortChange}
              />
            )
          )}
        </div>
      </div>
      {/* Icon rail — the panel menu (⋮), plus terminal and theme shortcuts.
          Panel switching and session actions live in the menu; the rail keeps
          only the always-useful shortcuts as fixed icons. */}
      <div className="flex w-[52px] shrink-0 flex-col items-center gap-1.5 border-l border-border bg-background/50 py-3">
        {showShellsTab && (
          <button
            type="button"
            aria-label="Terminais"
            title="Terminais"
            onClick={() => onRightRailTabChange("terminals")}
            className={railBtn}
          >
            <TerminalIcon className="size-[18px]" />
          </button>
        )}
        <button
          type="button"
          aria-label="Alternar tema"
          title="Tema"
          onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
          className={railBtn}
        >
          <SunMoonIcon className="size-[18px]" />
        </button>
        <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label="Menu do painel"
              className={cn(railBtn, "relative", menuOpen && "bg-primary/15 text-foreground")}
            >
              <EllipsisVerticalIcon className="size-[18px]" />
              <span className="absolute right-1.5 top-1.5 size-1.5 rounded-full bg-primary" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="left" className="w-60">
            <DropdownMenuLabel>Painel</DropdownMenuLabel>
            {panels.map((p) => {
              const on = selectedFilePath === null && p.id === rightRailTab;
              return (
                <DropdownMenuItem
                  key={p.id}
                  data-active={on ? "true" : "false"}
                  onSelect={() => onRightRailTabChange(p.id)}
                >
                  <p.Icon className="size-4" />
                  <span className="flex-1">{p.label}</span>
                  {p.badge && (
                    <span
                      className={cn(
                        TAB_BADGE_BASE,
                        p.badgeTint ?? "bg-muted text-muted-foreground",
                      )}
                    >
                      {p.badge}
                    </span>
                  )}
                  {on && <CheckIcon className="size-3.5 text-primary" />}
                </DropdownMenuItem>
              );
            })}
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Sessão</DropdownMenuLabel>
            <DropdownMenuItem onSelect={doRename}>
              <PencilIcon className="size-4" />
              Renomear
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={doArchive}>
              <ArchiveIcon className="size-4" />
              Arquivar
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </aside>
  );
}
