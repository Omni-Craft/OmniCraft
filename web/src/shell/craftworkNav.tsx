// Craftwork — a Cowork-style workspace surface, reached from the top-of-sidebar
// Chat / Craftwork switcher (mirrors Claude Desktop's Home / Code toggle).
//
// Like the Settings surface, entering /craftwork swaps the sidebar body for a
// section nav while the main area (CraftworkPage) renders the selected section.
// Section selection is URL-driven (/craftwork/<section>) so nav and content
// stay in sync without shared state.

import {
  ChartColumnIcon,
  ClipboardCheckIcon,
  ClockIcon,
  Code2Icon,
  GitPullRequestIcon,
  HomeIcon,
  LayoutGridIcon,
  PanelRightOpenIcon,
  SettingsIcon,
  SwordsIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link, useLocation } from "@/lib/routing";
import { cn } from "@/lib/utils";

export type CraftworkSectionId = "home" | "gallery" | "scheduled" | "evals" | "costs";

const SECTION_IDS: readonly CraftworkSectionId[] = [
  "home",
  "gallery",
  "scheduled",
  "evals",
  "costs",
];

/**
 * Parse the active route into a Craftwork descriptor. `inCraftwork` gates the
 * sidebar-body swap and the top switcher's active state; `section` drives the
 * content. Bare `/craftwork` defaults to the `home` hub.
 */
export function useCraftworkRoute(): { inCraftwork: boolean; section: CraftworkSectionId } {
  const segments = useLocation().pathname.split("/").filter(Boolean);
  const idx = segments.lastIndexOf("craftwork");
  if (idx === -1) return { inCraftwork: false, section: "home" };
  const next = segments[idx + 1];
  const section = (SECTION_IDS as readonly string[]).includes(next)
    ? (next as CraftworkSectionId)
    : "home";
  return { inCraftwork: true, section };
}

/** True when the route belongs to the "Code" surface (coding composer + a session). */
export function useInCode(): boolean {
  const segs = useLocation().pathname.split("/").filter(Boolean);
  return segs.includes("code") || segs.includes("c");
}

/**
 * Top-of-sidebar Início / Code switcher — the two primary destinations, styled
 * as a segmented pill (Claude Desktop's Home / Code pattern). Início is the
 * new-session home (where the composer's Chat / Craftwork toggle lives); Code is
 * your coding sessions.
 */
export function SidebarModeSwitcher({
  onNavClick,
}: {
  onNavClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
}) {
  const inCode = useInCode();
  const item = (active: boolean) =>
    cn(
      "flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
      active
        ? "bg-background text-foreground shadow-sm"
        : "text-muted-foreground hover:text-foreground",
    );
  return (
    <div className="px-3 pt-3">
      <div className="flex gap-1 rounded-lg bg-muted/60 p-1">
        <Link to="/" onClick={onNavClick} className={item(!inCode)} aria-current={!inCode}>
          <HomeIcon className="size-4" />
          Início
        </Link>
        <Link
          to="/code"
          onClick={onNavClick}
          className={item(inCode)}
          aria-current={inCode}
          data-testid="code-switch"
        >
          <Code2Icon className="size-4" />
          Code
        </Link>
      </div>
    </div>
  );
}

/**
 * The Início-tab sub-switch: Chat (the no-filesystem conversation composer at
 * "/") vs Craftwork (the agentic workspace hub at "/craftwork"). Both live under
 * the Início top tab. Rendered inside the Chat composer and atop the Craftwork
 * hub so you can flip between them.
 */
export function HomeModeToggle() {
  const { inCraftwork } = useCraftworkRoute();
  const item = (active: boolean) =>
    cn(
      "rounded-full px-3 py-1 text-xs font-medium transition-colors",
      active ? "bg-card text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
    );
  return (
    <div className="flex items-center rounded-full bg-muted p-0.5" data-testid="home-mode-toggle">
      <Link to="/" className={item(!inCraftwork)} aria-current={!inCraftwork}>
        Chat
      </Link>
      <Link
        to="/craftwork"
        className={item(inCraftwork)}
        aria-current={inCraftwork}
        data-testid="home-mode-craftwork"
      >
        Craftwork
      </Link>
    </div>
  );
}

interface NavItem {
  id: CraftworkSectionId;
  label: string;
  icon: typeof HomeIcon;
}

const NAV: NavItem[] = [
  { id: "home", label: "Visão geral", icon: HomeIcon },
  { id: "gallery", label: "Galeria de agentes", icon: LayoutGridIcon },
  { id: "scheduled", label: "Agentes agendados", icon: ClockIcon },
  { id: "evals", label: "Avaliações", icon: ClipboardCheckIcon },
  { id: "costs", label: "Custos", icon: ChartColumnIcon },
];

// Standalone agentic surfaces that live outside /craftwork but belong to the
// same workspace — linked out rather than embedded.
const LINKS: { to: string; label: string; icon: typeof HomeIcon }[] = [
  { to: "/arena", label: "Arena", icon: SwordsIcon },
  { to: "/github", label: "GitHub", icon: GitPullRequestIcon },
];

/**
 * Sidebar body rendered inside the card when on /craftwork — a section nav
 * (mirrors SettingsSidebarBody), plus links to the standalone surfaces and a
 * Settings footer. The Chat / Craftwork switcher above it stays mounted.
 */
export function CraftworkSidebarBody({
  onNavClick,
  onClose,
}: {
  onNavClick: (e: React.MouseEvent<HTMLAnchorElement>) => void;
  onClose: () => void;
}) {
  const { section } = useCraftworkRoute();

  const navBtn = (to: string, label: string, Icon: typeof HomeIcon, selected: boolean) => (
    <Button
      asChild
      variant="ghost"
      className={cn("w-full justify-start gap-2 text-sm", selected && "bg-muted font-semibold")}
    >
      <Link to={to} onClick={onNavClick} aria-current={selected ? "page" : undefined}>
        <Icon className="size-4 text-muted-foreground" />
        {label}
      </Link>
    </Button>
  );

  return (
    <>
      <div className="flex items-center justify-between px-4 pt-3">
        <span className="text-[15px] font-semibold tracking-tight">Craftwork</span>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label="Fechar barra lateral"
              onClick={onClose}
              className="rounded-full"
            >
              <PanelRightOpenIcon className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="bottom">Recolher barra lateral</TooltipContent>
        </Tooltip>
      </div>

      <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-3 py-3">
        {NAV.map((it) => navBtn(`/craftwork/${it.id}`, it.label, it.icon, section === it.id))}
        <div className="my-2 border-border/60 border-t" />
        {LINKS.map((it) => navBtn(it.to, it.label, it.icon, false))}
      </nav>

      <div className="shrink-0 px-3 pb-3">
        {navBtn("/settings", "Configurações", SettingsIcon, false)}
      </div>
    </>
  );
}
