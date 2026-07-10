/**
 * Settings page (``/settings``).
 *
 * Renders into the AppShell chat outlet (see App.tsx) so the conversations
 * sidebar stays put when you enter settings — only the main area swaps to
 * this view. Inside, a section nav (left) drives a content panel (right),
 * modeled on a desktop-app settings window; a "← Back to Omnigent" link
 * returns to the composer.
 *
 * Sections:
 *
 * - **Appearance** — theme mode (System / Light / Dark). This is the new
 *   home of the theme control that used to sit in the sidebar header.
 * - **Git** — Git behavior, e.g. the default base branch pre-filled when
 *   naming a new worktree branch in the composer.
 * - **Keyboard shortcuts** — the full shortcuts reference, shown inline.
 * - **Account** — only when the accounts auth provider is active. Absorbs
 *   the old sidebar AccountMenu: signed-in identity, change password, and
 *   sign out.
 * - **Members** / **Policies** — admin-only, accounts deploys. Server-wide
 *   management surfaces rendered as settings sub-categories (previously
 *   standalone `/members` and `/policies` pages linked from Account) so
 *   entering them stays inside settings — the sidebar keeps the section nav
 *   instead of snapping back to the conversation list.
 * - **Archived sessions** — archived sessions, moved out of the sidebar
 *   list. Not clickable; each row reveals Delete / Unarchive on hover.
 */

import {
  lazy,
  type ReactNode,
  Suspense,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  ArchiveRestoreIcon,
  KeyRoundIcon,
  LogOutIcon,
  Trash2Icon,
  UserCogIcon,
} from "lucide-react";
import {
  CheckIcon,
  LaptopMinimalIcon,
  MinusIcon,
  MonitorIcon,
  MoonIcon,
  PlusIcon,
  SunIcon,
} from "lucide-react";
import { useTheme } from "next-themes";
import { PageScroll } from "@/components/PageScroll";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { KeyboardShortcutsList } from "@/components/KeyboardShortcutsDialog";
import { changePassword, logout } from "@/lib/accountsApi";
import { getCurrentIsAdmin, resolveIdentity } from "@/lib/identity";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  type Conversation,
  useArchiveConversation,
  useConversations,
  useStopAndDeleteConversation,
} from "@/hooks/useConversations";
import { conversationDisplayLabel } from "@/shell/sidebarNav";
import { absoluteTime } from "@/lib/relativeTime";
import { useSettingsRoute } from "@/shell/settingsNav";
import {
  normalizeResolvedTheme,
  normalizeThemeMode,
  type ThemeMode,
} from "@/components/theme/themeMode";
import {
  applyUiFontFamily,
  applyUiFontScale,
  clampUiFontSizePx,
  readUiFontFamily,
  readUiFontSizePx,
  UI_FONT_FAMILY_DEFAULT,
  UI_FONT_SIZE_MAX,
  UI_FONT_SIZE_MIN,
  UI_FONT_SIZE_STEP,
  writeUiFontFamily,
  writeUiFontSizePx,
} from "@/lib/uiFontPreferences";
import {
  clampCodeFontSizePx,
  CODE_FONT_FAMILY_DEFAULT,
  CODE_FONT_SIZE_MAX,
  CODE_FONT_SIZE_MIN,
  CODE_FONT_SIZE_STEP,
  readCodeFontFamily,
  readCodeFontSizePx,
  writeCodeFontFamily,
  writeCodeFontSizePx,
} from "@/lib/codeFontPreferences";
import {
  readTerminalThemeMode,
  writeTerminalThemeMode,
  type TerminalThemeMode,
} from "@/lib/terminalThemePreferences";
import { readDefaultBaseBranch, writeDefaultBaseBranch } from "@/lib/baseBranchPreferences";
import {
  applyThemePalette,
  isThemePalette,
  PALETTES,
  type PaletteSwatch,
  readThemePalette,
  type ThemePalette,
  writeThemePalette,
} from "@/lib/themePalette";
import { useIsEmbedded } from "@/lib/embedded";
import { type CliStatus, getCliStatus, isElectronShell, resetCliPath } from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

// Admin-only management surfaces, rendered as the Members / Policies settings
// sub-categories. Visible to admins in all modes (accounts, OIDC, single-user).
// Lazy-loaded to keep the settings chunk small.
const MembersPage = lazy(() =>
  import("@/pages/MembersPage").then((m) => ({ default: m.MembersPage })),
);
const PoliciesPage = lazy(() =>
  import("@/pages/PoliciesPage").then((m) => ({ default: m.PoliciesPage })),
);

/**
 * Settings content panel. The section nav lives in the sidebar card
 * (SettingsSidebarBody); this renders only the selected section into the
 * AppShell main outlet. The active section is read from the URL so the two
 * stay in sync. PageScroll handles clearing the shell's absolute header and
 * the iOS native bars, matching the Inbox / Members pages.
 */
export function SettingsPage() {
  const info = useServerInfo();
  // A login session exists (accounts OR OIDC) when the server advertises a
  // login_url; gates the Account section so SSO users get it too.
  const hasAuthSession = info !== "loading" && info.login_url !== null;
  const { section } = useSettingsRoute();

  // Members / Policies are admin-only management surfaces that own their full
  // layout (their own PageScroll + admin gating), so they render directly —
  // NOT inside the shared section PageScroll below, which would nest two
  // scroll containers. Both self-gate to admins server-side and client-side.
  // Rendered in ANY multi-user mode (accounts AND OIDC), not gated on
  // `accountsEnabled` — the nav + pages handle admin gating, and Members runs
  // read-only under OIDC (no password actions).
  if (section === "members" || section === "policies") {
    return (
      <Suspense fallback={null}>
        {section === "members" ? <MembersPage /> : <PoliciesPage />}
      </Suspense>
    );
  }

  return (
    <PageScroll contentClassName="px-8" extraBottom="2.5rem">
      {section === "appearance" && <AppearanceSection />}
      {section === "git" && <GitSection />}
      {section === "shortcuts" && <ShortcutsSection />}
      {section === "account" && hasAuthSession && <AccountSection />}
      {section === "archived" && <ArchivedSection />}
      {section === "cli" && isElectronShell() && <LocalCliSection />}
    </PageScroll>
  );
}

/** Shared section shell: a title + optional description above the body. */
function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      <div className="mt-6">{children}</div>
    </section>
  );
}

const themeCards: { mode: ThemeMode; label: string; icon: typeof SunIcon }[] = [
  { mode: "system", label: "Sistema", icon: LaptopMinimalIcon },
  { mode: "light", label: "Claro", icon: SunIcon },
  { mode: "dark", label: "Escuro", icon: MoonIcon },
];

const terminalThemeCards: { mode: TerminalThemeMode; label: string; icon: typeof SunIcon }[] = [
  { mode: "auto", label: "Seguir o app", icon: MonitorIcon },
  { mode: "light", label: "Claro", icon: SunIcon },
  { mode: "dark", label: "Escuro", icon: MoonIcon },
];

/**
 * Checkmark badge pinned to the top-right corner of a selected card. Shared by
 * every appearance radiogroup so "selected" reads identically everywhere.
 */
function SelectedBadge() {
  return (
    <span
      aria-hidden
      className="absolute right-1.5 top-1.5 flex size-4 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm"
    >
      <CheckIcon className="size-3" />
    </span>
  );
}

/**
 * Shared card styling for the appearance radiogroups. Selected cards carry the
 * accent border + a subtle accent wash (paired with <SelectedBadge/>); the rest
 * highlight their border and lift on hover. focus-visible keeps the global
 * outline ring, so keyboard focus stays visually distinct from selection.
 */
function themeCardClass(selected: boolean, layout?: string) {
  return cn(
    "relative flex flex-col rounded-lg border-2 transition-[color,background-color,border-color,box-shadow]",
    selected
      ? "border-primary bg-primary/5"
      : "border-border hover:border-border-strong hover:bg-muted hover:shadow-sm",
    layout,
  );
}

/** Centered icon + label body shared by the Mode and Terminal theme cards. */
function iconCardBody(Icon: typeof SunIcon, label: string) {
  return (
    <>
      <Icon className="size-6 text-muted-foreground" />
      <span className="text-sm font-medium">{label}</span>
    </>
  );
}

// Neutral light/dark window tones for the Mode preview tiles. These are about
// light-vs-dark only (not the color theme), so they stay grayscale.
const LIGHT_MODE_PREVIEW: PaletteSwatch = {
  bg: "#e9ebee",
  card: "#ffffff",
  accent: "#aab2bd",
  border: "#d7dbe0",
  text: "#11171c",
};
const DARK_MODE_PREVIEW: PaletteSwatch = {
  bg: "#0d1218",
  card: "#232a33",
  accent: "#5b6672",
  border: "#2b333d",
  text: "#e6edf3",
};

/**
 * Mini app-window mock for a Mode tile, reusing {@link PaletteSwatchPreview}. A
 * light or dark two-pane window; "system" shows one window split diagonally —
 * light on the near side, dark on the far — to signal "follow the OS".
 */
function ModePreview({ variant }: { variant: ThemeMode }) {
  if (variant === "light") return <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />;
  if (variant === "dark") return <PaletteSwatchPreview swatch={DARK_MODE_PREVIEW} />;
  return (
    <div className="relative h-16 w-full">
      <PaletteSwatchPreview swatch={LIGHT_MODE_PREVIEW} />
      <div
        aria-hidden
        className="absolute inset-0"
        style={{ clipPath: "polygon(62% 0, 100% 0, 100% 100%, 38% 100%)" }}
      >
        <PaletteSwatchPreview swatch={DARK_MODE_PREVIEW} />
      </div>
    </div>
  );
}

/** Small swatch chip (canvas + accent dot) for the color-theme dropdown. */
function PaletteChip({ swatch }: { swatch: PaletteSwatch }) {
  return (
    <span
      aria-hidden
      className="flex size-5 shrink-0 items-center justify-center rounded-md border"
      style={{ backgroundColor: swatch.bg, borderColor: swatch.border }}
    >
      <span className="size-2 rounded-full" style={{ backgroundColor: swatch.accent }} />
    </span>
  );
}

/** One option in a {@link CardRadioGroup}. */
interface CardRadioOption<T extends string> {
  value: T;
  testId: string;
  body: ReactNode;
  /** Optional native tooltip (used for the palette blurbs). */
  title?: string;
}

/**
 * Accessible card radiogroup shared by all three appearance pickers. Implements
 * the WAI-ARIA radiogroup pattern: a roving tabindex (only the selected card is
 * tabbable), arrow keys move selection within the group, and Enter/Space select
 * the focused card. `labelledBy` points at the subsection heading so the group's
 * accessible name matches its visible label.
 */
function CardRadioGroup<T extends string>({
  labelledBy,
  value,
  onSelect,
  items,
  className,
  cardClassName,
}: {
  labelledBy: string;
  value: T;
  onSelect: (value: T) => void;
  items: readonly CardRadioOption<T>[];
  className?: string;
  cardClassName?: string;
}) {
  // Keep a handle on each card so arrow-key navigation can move focus as it
  // moves selection (selection-follows-focus, per the radiogroup pattern).
  const refs = useRef(new Map<T, HTMLButtonElement | null>());

  return (
    <div role="radiogroup" aria-labelledby={labelledBy} className={className}>
      {items.map((item, index) => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            ref={(el) => {
              refs.current.set(item.value, el);
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            tabIndex={selected ? 0 : -1}
            title={item.title}
            data-testid={item.testId}
            onClick={() => onSelect(item.value)}
            onKeyDown={(event) => {
              const forward = event.key === "ArrowRight" || event.key === "ArrowDown";
              const backward = event.key === "ArrowLeft" || event.key === "ArrowUp";
              if (!forward && !backward) return;
              event.preventDefault();
              const nextIndex = (index + (forward ? 1 : -1) + items.length) % items.length;
              const next = items[nextIndex].value;
              onSelect(next);
              refs.current.get(next)?.focus();
            }}
            className={themeCardClass(selected, cardClassName)}
          >
            {selected && <SelectedBadge />}
            {item.body}
          </button>
        );
      })}
    </div>
  );
}

/** A labeled Appearance subsection: heading + one-line helper + its control. */
function ThemeSubsection({
  labelId,
  title,
  helper,
  children,
}: {
  labelId: string;
  title: string;
  helper: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col">
        <span id={labelId} className="text-sm font-medium">
          {title}
        </span>
        <span className="text-sm text-muted-foreground">{helper}</span>
      </div>
      {children}
    </div>
  );
}

/** Appearance mode: System / Light / Dark. */
function ModeControl() {
  const { theme, setTheme } = useTheme();
  const mode = normalizeThemeMode(theme);
  const labelId = useId();
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Modo"
      helper="Siga o sistema, ou force claro ou escuro."
    >
      <CardRadioGroup<ThemeMode>
        labelledBy={labelId}
        value={mode}
        onSelect={(next) => setTheme(next)}
        className="grid grid-cols-3 gap-3"
        cardClassName="gap-2 p-2"
        items={themeCards.map((card) => ({
          value: card.mode,
          testId: `theme-${card.mode}`,
          body: (
            <>
              <ModePreview variant={card.mode} />
              <span className="text-center text-sm font-medium">{card.label}</span>
            </>
          ),
        }))}
      />
    </ThemeSubsection>
  );
}

/** Terminal light/dark/match-app theme — its own section. */
function TerminalThemeControl() {
  const [mode, setMode] = useState(() => readTerminalThemeMode());
  const labelId = useId();
  const choose = useCallback((next: TerminalThemeMode) => {
    setMode(next);
    writeTerminalThemeMode(next);
  }, []);
  return (
    <ThemeSubsection
      labelId={labelId}
      title="Tema do terminal"
      helper="Use um terminal claro ou escuro, ou siga o app."
    >
      <CardRadioGroup<TerminalThemeMode>
        labelledBy={labelId}
        value={mode}
        onSelect={choose}
        className="grid grid-cols-3 gap-3"
        cardClassName="items-center gap-2 p-4"
        items={terminalThemeCards.map((card) => ({
          value: card.mode,
          testId: `terminal-theme-${card.mode}`,
          body: iconCardBody(card.icon, card.label),
        }))}
      />
    </ThemeSubsection>
  );
}

/**
 * Color-theme (palette) picker — a dropdown (à la Codex). Each option shows a
 * swatch chip + name and the trigger mirrors the current selection. Choosing
 * one applies it live to <html> via `data-theme`, persists it, and composes on
 * top of the chosen light/dark mode.
 */
function ColorThemeControl() {
  // Render each chip in the currently-resolved mode so it matches the app now.
  const { resolvedTheme } = useTheme();
  const isDark = normalizeResolvedTheme(resolvedTheme) === "dark";
  const [palette, setPalette] = useState<ThemePalette>(() => readThemePalette());
  const labelId = useId();

  const choose = useCallback((next: ThemePalette) => {
    setPalette(next);
    writeThemePalette(next);
    applyThemePalette(next);
  }, []);

  const selected = PALETTES.find((p) => p.id === palette) ?? PALETTES[0];

  return (
    <ThemeSubsection
      labelId={labelId}
      title="Tema de cores"
      helper="Aplica-se sobre o modo escolhido."
    >
      <Select
        value={palette}
        onValueChange={(next) => {
          if (isThemePalette(next)) choose(next);
        }}
      >
        <SelectTrigger
          aria-labelledby={labelId}
          data-testid="color-theme-select"
          className="w-full max-w-xs gap-2"
        >
          <SelectValue>
            <PaletteChip swatch={isDark ? selected.dark : selected.light} />
            <span>{selected.label}</span>
          </SelectValue>
        </SelectTrigger>
        <SelectContent>
          {PALETTES.map((p) => (
            <SelectItem key={p.id} value={p.id} data-testid={`palette-${p.id}`}>
              <PaletteChip swatch={isDark ? p.dark : p.light} />
              <span>{p.label}</span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </ThemeSubsection>
  );
}

/**
 * Miniature "app window" preview for a palette: a canvas with a small sidebar
 * and content card, a few text lines, and an accent chip — built purely from
 * the swatch colors so each palette reads at a glance.
 */
function PaletteSwatchPreview({ swatch }: { swatch: PaletteSwatch }) {
  return (
    <div
      aria-hidden
      className="flex h-16 w-full gap-1.5 overflow-hidden rounded-lg p-1.5"
      style={{ backgroundColor: swatch.bg, border: `1px solid ${swatch.border}` }}
    >
      <div
        className="flex w-1/3 flex-col gap-1 rounded-md p-1"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <div className="size-1.5 rounded-full" style={{ backgroundColor: swatch.accent }} />
        <div
          className="h-1 w-4/5 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.35 }}
        />
        <div
          className="h-1 w-3/5 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.25 }}
        />
      </div>
      <div
        className="flex flex-1 flex-col gap-1 rounded-md p-1.5"
        style={{ backgroundColor: swatch.card, border: `1px solid ${swatch.border}` }}
      >
        <div
          className="h-1 w-3/4 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.5 }}
        />
        <div
          className="h-1 w-1/2 rounded-full"
          style={{ backgroundColor: swatch.text, opacity: 0.3 }}
        />
        <div className="mt-auto h-2.5 w-2/5 rounded" style={{ backgroundColor: swatch.accent }} />
      </div>
    </div>
  );
}

function AppearanceSection() {
  // Embedded: the host owns light/dark, so the Mode and Color theme pickers
  // would be no-ops — hide them and say so (matching ThemeModeMenu). Terminal
  // theme and the font controls are per-device prefs that don't conflict with
  // host theming, so they stay visible.
  const isEmbedded = useIsEmbedded();

  return (
    <Section title="Aparência" description="Escolha como o OmniCraft aparece neste dispositivo.">
      <div className="flex flex-col gap-8">
        {isEmbedded ? (
          <div className="flex flex-col gap-3">
            <span className="text-sm font-medium">Tema</span>
            <p className="text-sm text-muted-foreground">
              O tema é controlado pelo aplicativo host.
            </p>
          </div>
        ) : (
          <>
            <ModeControl />
            <ColorThemeControl />
          </>
        )}

        <TerminalThemeControl />

        <UiFontSizeControl />

        <UiFontFamilyControl />

        {/* Code font (Monaco + xterm) sits as its own two rows — labelled in full
            ("Code font size" / "Code font family") rather than under a shared
            heading — so each control reads unambiguously next to the UI-font rows
            above and it's clear these don't scale the surrounding chrome. */}
        <UiCodeFontSizeControl />

        <UiCodeFontFamilyControl />
      </div>
    </Section>
  );
}

/** Git behavior settings. */
function GitSection() {
  return (
    <Section title="Git" description="Configure como o OmniCraft trabalha com o Git.">
      <div className="flex flex-col gap-8">
        <DefaultBaseBranchControl />
      </div>
    </Section>
  );
}

/**
 * Default base branch for new worktrees. When set, the new-session composer
 * pre-fills the base-branch field as you name a new branch, so the worktree
 * branches off it. Leave blank to keep the field empty (worktrees default to
 * the current branch).
 */
function DefaultBaseBranchControl() {
  const [branch, setBranch] = useState(() => readDefaultBaseBranch() ?? "");

  const update = useCallback((next: string) => {
    setBranch(next);
    writeDefaultBaseBranch(next);
  }, []);

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">Branch base padrão</span>
        <span className="text-sm text-muted-foreground">
          Preenchido automaticamente como base ao nomear um novo branch de worktree. Deixe em
          branco para não preencher automaticamente.
        </span>
      </div>
      <Input
        type="text"
        aria-label="Branch base padrão"
        data-testid="settings-default-base-branch-input"
        placeholder="ex.: main"
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        className="h-9 w-56 shrink-0"
        value={branch}
        onChange={(e) => update(e.target.value)}
      />
    </div>
  );
}

/**
 * UI font size stepper. Scales the whole rem-based UI via the --ui-font-scale
 * variable (see lib/uiFontPreferences.ts). Applied live and persisted on every
 * change; unlike the theme picker it stays visible when embedded, since it's a
 * per-device readability pref that doesn't conflict with host theming.
 */
function UiFontSizeControl() {
  // `px` is the committed value: clamped, persisted, and applied to the UI.
  // `draft` is the raw text in the box, kept separate so mid-edit states the
  // committed value can't hold — a transient out-of-range number (e.g. "1" on
  // the way to "18") or an empty field while retyping — don't get clamped on
  // every keystroke. We only commit while typing when the draft is already a
  // valid in-range size; blur/Enter clamps and re-syncs the text.
  const [px, setPx] = useState(() => readUiFontSizePx());
  const [draft, setDraft] = useState(() => String(px));

  const commit = useCallback((next: number) => {
    const clamped = clampUiFontSizePx(next);
    setPx(clamped);
    setDraft(String(clamped));
    writeUiFontSizePx(clamped);
    applyUiFontScale(clamped);
  }, []);

  const onDraftChange = useCallback((text: string) => {
    setDraft(text);
    // Apply live only once the field holds a valid, in-range whole number;
    // leave partial/out-of-range/empty drafts untouched until blur.
    if (/^\d+$/.test(text)) {
      const value = Number(text);
      if (value >= UI_FONT_SIZE_MIN && value <= UI_FONT_SIZE_MAX) {
        setPx(value);
        writeUiFontSizePx(value);
        applyUiFontScale(value);
      }
    }
  }, []);

  // Clamp and re-sync the text to the committed value. An empty or invalid
  // draft reverts to the last committed size rather than a bogus one.
  const commitDraft = useCallback(() => {
    const value = Number(draft);
    commit(Number.isFinite(value) && draft.trim() !== "" ? value : px);
  }, [commit, draft, px]);

  const atMin = px <= UI_FONT_SIZE_MIN;
  const atMax = px >= UI_FONT_SIZE_MAX;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex flex-col">
        <span className="text-sm font-medium">Tamanho da fonte</span>
        <span className="text-sm text-muted-foreground">
          Ajusta o tamanho do texto e o espaçamento da interface neste dispositivo.
        </span>
      </div>
      {/* One cohesive pill: [ −  | value px |  + ]. Segments share the pill
          border via inner dividers rather than floating as separate boxes. */}
      <div
        role="group"
        aria-label="Tamanho da fonte"
        className={cn(
          "inline-flex h-9 items-stretch overflow-hidden rounded-lg border border-input bg-background transition-colors dark:bg-input/30",
          "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        )}
      >
        <StepperButton
          label="Diminuir tamanho da fonte"
          testId="ui-font-size-dec"
          disabled={atMin}
          onClick={() => commit(px - UI_FONT_SIZE_STEP)}
        >
          <MinusIcon className="size-4" />
        </StepperButton>
        <div className="flex items-center border-x border-input px-2 tabular-nums">
          <input
            type="number"
            inputMode="numeric"
            min={UI_FONT_SIZE_MIN}
            max={UI_FONT_SIZE_MAX}
            step={UI_FONT_SIZE_STEP}
            aria-label="Tamanho da fonte em pixels"
            data-testid="ui-font-size-input"
            className="w-8 bg-transparent text-center text-sm font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
        </div>
        <StepperButton
          label="Aumentar tamanho da fonte"
          testId="ui-font-size-inc"
          disabled={atMax}
          onClick={() => commit(px + UI_FONT_SIZE_STEP)}
        >
          <PlusIcon className="size-4" />
        </StepperButton>
      </div>
    </div>
  );
}

/**
 * UI font family picker. Free-text (Cursor-style): type any font installed on
 * this device; blank means "System default", which falls back to the existing
 * --font-sans stack. Applies live and persists on every change via the
 * --ui-font-family variable (see lib/uiFontPreferences.ts). Like the size
 * control it stays visible when embedded — a per-device readability pref that
 * doesn't conflict with host theming.
 */
function UiFontFamilyControl() {
  const [family, setFamily] = useState(() => readUiFontFamily());

  const update = useCallback((next: string) => {
    setFamily(next);
    writeUiFontFamily(next);
    applyUiFontFamily(next);
  }, []);

  const isDefault = family.trim() === UI_FONT_FAMILY_DEFAULT;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      {/* Take the remaining width (and let the longer description wrap within
          this column) so the input stays inline instead of dropping to its own
          row — matches the font-size row's alignment. */}
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">Família da fonte</span>
        <span className="text-sm text-muted-foreground">
          Use qualquer fonte instalada neste dispositivo. Deixe em branco para o padrão do
          sistema.
        </span>
      </div>
      {/* Reset sits left of the input so the input is the rightmost element and
          its right edge lines up flush with the font-size stepper above.
          `invisible` (not removed) at the default keeps the row from shifting. */}
      <div role="group" aria-label="Família da fonte" className="flex shrink-0 items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          data-testid="ui-font-family-reset"
          disabled={isDefault}
          className={cn("h-9", isDefault && "invisible")}
          onClick={() => update(UI_FONT_FAMILY_DEFAULT)}
        >
          Redefinir
        </Button>
        <Input
          type="text"
          aria-label="Família da fonte da interface"
          data-testid="ui-font-family-input"
          placeholder="Padrão do sistema"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="h-9 w-56"
          value={family}
          onChange={(e) => update(e.target.value)}
        />
      </div>
    </div>
  );
}

/**
 * Code font size stepper. Sizes the code editor (Monaco) and terminal (xterm)
 * — fixed-pixel widgets that can't ride the chrome's --ui-font-scale variable,
 * so writing the pref emits to already-mounted editors/terminals (see
 * lib/codeFontPreferences.ts). Same free-editing draft/commit + blur-clamp
 * behavior as UiFontSizeControl; only the bounds and storage differ.
 */
function UiCodeFontSizeControl() {
  // `px` is the committed value; `draft` is the raw text in the box, kept
  // separate so a transient out-of-range/empty mid-edit state isn't clamped or
  // persisted on every keystroke. We only commit while typing when the draft is
  // already a valid in-range size; blur/Enter clamps and re-syncs the text.
  const [px, setPx] = useState(() => readCodeFontSizePx());
  const [draft, setDraft] = useState(() => String(px));

  const commit = useCallback((next: number) => {
    const clamped = clampCodeFontSizePx(next);
    setPx(clamped);
    setDraft(String(clamped));
    writeCodeFontSizePx(clamped);
  }, []);

  const onDraftChange = useCallback((text: string) => {
    setDraft(text);
    // Apply live only once the field holds a valid, in-range whole number;
    // leave partial/out-of-range/empty drafts untouched until blur.
    if (/^\d+$/.test(text)) {
      const value = Number(text);
      if (value >= CODE_FONT_SIZE_MIN && value <= CODE_FONT_SIZE_MAX) {
        setPx(value);
        writeCodeFontSizePx(value);
      }
    }
  }, []);

  // Clamp and re-sync the text to the committed value. An empty or invalid
  // draft reverts to the last committed size rather than a bogus one.
  const commitDraft = useCallback(() => {
    const value = Number(draft);
    commit(Number.isFinite(value) && draft.trim() !== "" ? value : px);
  }, [commit, draft, px]);

  const atMin = px <= CODE_FONT_SIZE_MIN;
  const atMax = px >= CODE_FONT_SIZE_MAX;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex flex-col">
        <span className="text-sm font-medium">Tamanho da fonte do código</span>
        <span className="text-sm text-muted-foreground">
          Tamanho do código no editor e no terminal.
        </span>
      </div>
      {/* One cohesive pill: [ −  | value px |  + ] — same shell as the UI
          font-size control. */}
      <div
        role="group"
        aria-label="Tamanho da fonte do código"
        className={cn(
          "inline-flex h-9 items-stretch overflow-hidden rounded-lg border border-input bg-background transition-colors dark:bg-input/30",
          "focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50",
        )}
      >
        <StepperButton
          label="Diminuir tamanho da fonte do código"
          testId="code-font-size-dec"
          disabled={atMin}
          onClick={() => commit(px - CODE_FONT_SIZE_STEP)}
        >
          <MinusIcon className="size-4" />
        </StepperButton>
        <div className="flex items-center border-x border-input px-2 tabular-nums">
          <input
            type="number"
            inputMode="numeric"
            min={CODE_FONT_SIZE_MIN}
            max={CODE_FONT_SIZE_MAX}
            step={CODE_FONT_SIZE_STEP}
            aria-label="Tamanho da fonte do código em pixels"
            data-testid="code-font-size-input"
            className="w-8 bg-transparent text-center text-sm font-medium tabular-nums outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onBlur={commitDraft}
            onKeyDown={(e) => {
              if (e.key === "Enter") e.currentTarget.blur();
            }}
          />
        </div>
        <StepperButton
          label="Aumentar tamanho da fonte do código"
          testId="code-font-size-inc"
          disabled={atMax}
          onClick={() => commit(px + CODE_FONT_SIZE_STEP)}
        >
          <PlusIcon className="size-4" />
        </StepperButton>
      </div>
    </div>
  );
}

/**
 * Code font family picker. Free-text (Cursor-style): type any monospace font
 * installed on this device; blank means the editor/terminal default (the shared
 * mono stack). Applies live and persists on every change via the code-font
 * pub/sub (see lib/codeFontPreferences.ts). Mirrors UiFontFamilyControl.
 */
function UiCodeFontFamilyControl() {
  const [family, setFamily] = useState(() => readCodeFontFamily());

  const update = useCallback((next: string) => {
    setFamily(next);
    writeCodeFontFamily(next);
  }, []);

  const isDefault = family.trim() === CODE_FONT_FAMILY_DEFAULT;

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="text-sm font-medium">Família da fonte do código</span>
        <span className="text-sm text-muted-foreground">
          Fonte para o editor de código e terminal. Deixe em branco para o padrão.
        </span>
      </div>
      {/* Reset sits left of the input so the input's right edge lines up flush
          with the size stepper above. `invisible` (not removed) at the default
          keeps the row from shifting. */}
      <div
        role="group"
        aria-label="Família da fonte do código"
        className="flex shrink-0 items-center gap-2"
      >
        <Button
          type="button"
          variant="ghost"
          size="sm"
          data-testid="code-font-family-reset"
          disabled={isDefault}
          className={cn("h-9", isDefault && "invisible")}
          onClick={() => update(CODE_FONT_FAMILY_DEFAULT)}
        >
          Redefinir
        </Button>
        <Input
          type="text"
          aria-label="Família da fonte do código"
          data-testid="code-font-family-input"
          placeholder="Padrão do editor"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          className="h-9 w-56"
          value={family}
          onChange={(e) => update(e.target.value)}
        />
      </div>
    </div>
  );
}

/** Flanking +/- segment of the font-size pill: square, ghost-hover, no border. */
function StepperButton({
  label,
  testId,
  disabled,
  onClick,
  children,
}: {
  label: string;
  testId: string;
  disabled: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      data-testid={testId}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex w-9 items-center justify-center text-muted-foreground transition-colors",
        "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
        "disabled:pointer-events-none disabled:opacity-40",
      )}
    >
      {children}
    </button>
  );
}

function ShortcutsSection() {
  return (
    <Section title="Atalhos de teclado" description="Acelere ações comuns com o teclado.">
      <KeyboardShortcutsList />
    </Section>
  );
}

/**
 * Desktop-only: shows which Omnigent CLI binary the shell resolved
 * (auto-detected or a custom override). Read-only — setting a custom path is
 * done on the connect/setup screen (the trusted surface that allows free-text
 * entry); the SPA exposes no path setter. A safe "reset to auto-detected" stays
 * here since it chooses no path.
 */
function LocalCliSection() {
  const [status, setStatus] = useState<CliStatus | null | "loading">("loading");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void getCliStatus().then(setStatus);
  }, []);

  const onReset = useCallback(async () => {
    setBusy(true);
    const next = await resetCliPath();
    setBusy(false);
    if (next) setStatus(next); // null only when the bridge is missing (old shell)
  }, []);

  if (status === "loading") {
    return (
      <Section title="CLI local">
        <p className="text-sm text-muted-foreground">Verificando…</p>
      </Section>
    );
  }

  return (
    <Section
      title="CLI local"
      description="A ferramenta de linha de comando do OmniCraft que este app usa para executar um servidor local e conectar esta máquina como um runner."
    >
      {status === null ? (
        <p className="text-sm text-muted-foreground">O status da CLI não está disponível.</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-2 text-sm">
            <span
              aria-hidden
              className={cn(
                "size-2 rounded-full",
                status.installed ? "bg-success" : "bg-muted-foreground/40",
              )}
            />
            <span>
              {status.installed
                ? `Encontrado${status.version ? ` · ${status.version}` : ""}`
                : "Não encontrado"}
            </span>
          </div>

          {status.path ? (
            <div className="flex flex-col gap-1">
              <span className="text-xs text-muted-foreground">
                {status.source === "configured"
                  ? "Caminho (personalizado)"
                  : "Caminho (detectado automaticamente)"}
              </span>
              <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                {status.path}
              </code>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              <p className="text-sm text-muted-foreground">
                A CLI do OmniCraft não foi encontrada. Instale-a e depois defina o caminho dela na
                tela de conexão:
              </p>
              {status.installCommand && (
                <code className="block overflow-x-auto rounded-md border border-border bg-muted/40 px-3 py-2 text-xs">
                  {status.installCommand}
                </code>
              )}
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            Por segurança, um caminho personalizado só pode ser definido na tela de conexão — isso
            impede que um servidor conectado aponte o app para um binário diferente. Abra-a no menu
            Servidor (Trocar servidor…) e use a engrenagem de configurações.
          </p>

          {status.source === "configured" && (
            <div>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => void onReset()}>
                Redefinir para detecção automática
              </Button>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

function AccountSection() {
  const info = useServerInfo();
  const accountsEnabled = info !== "loading" && info.accounts_enabled;
  // Identity for display. Sourced from the mode-agnostic `/v1/me` probe so it
  // works under OIDC too (the accounts-only `/auth/me` doesn't exist there).
  const [me, setMe] = useState<{ id: string; is_admin: boolean } | null | "unknown">("unknown");

  // Change-password dialog state (lifted verbatim from the old AccountMenu).
  // Only used in accounts mode — OIDC identities have no local password.
  const [pwOpen, setPwOpen] = useState(false);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  useEffect(() => {
    void (async () => {
      const userId = await resolveIdentity();
      setMe(userId === null ? null : { id: userId, is_admin: getCurrentIsAdmin() });
    })();
  }, []);

  const onSignOut = useCallback(async () => {
    if (accountsEnabled) {
      // Accounts: clear the cookie via the JSON logout endpoint, then land on
      // the SPA login form.
      await logout();
      // Hard navigation so the chat store / react-query cache reset.
      window.location.href = "/login";
      return;
    }
    // OIDC: logout is a server-side GET redirect at /auth/logout that clears
    // the session cookie (and honors the IdP end-session endpoint when
    // configured). A hard navigation lets the browser follow it and resets
    // client caches.
    window.location.href = "/auth/logout";
  }, [accountsEnabled]);

  const resetPwForm = useCallback(() => {
    setOldPw("");
    setNewPw("");
    setConfirmPw("");
    setPwError(null);
    setPwDone(false);
    setPwBusy(false);
  }, []);

  const onSubmitPassword = useCallback(async () => {
    if (newPw !== confirmPw) {
      setPwError("As novas senhas não coincidem.");
      return;
    }
    setPwBusy(true);
    setPwError(null);
    const result = await changePassword({ old_password: oldPw, new_password: newPw });
    setPwBusy(false);
    if (result.ok) {
      setPwDone(true);
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
    } else {
      setPwError(result.error);
    }
  }, [oldPw, newPw, confirmPw]);

  if (me === "unknown" || me === null) {
    return <Section title="Conta">{null}</Section>;
  }

  return (
    <Section title="Conta">
      <div className="flex flex-col gap-6">
        <div className="flex items-center gap-3">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-md border border-border">
            <UserCogIcon className="size-5" />
          </span>
          <div className="min-w-0">
            <div className="truncate font-medium">
              {me.id}
              {me.is_admin && (
                <span className="ml-1 text-xs font-normal text-muted-foreground">(admin)</span>
              )}
            </div>
          </div>
        </div>

        {/* Members / Policies used to live here as links to standalone pages.
            They're now first-class settings sub-categories in the sidebar nav
            (Admin group), so entering them keeps the settings surface put
            instead of navigating away from /settings. */}

        <div className="flex flex-col gap-1">
          {/* Change password is accounts-only — an OIDC identity's password
              lives with the IdP, so there's nothing to change here. */}
          {accountsEnabled && (
            <Button
              variant="ghost"
              className="w-full justify-start gap-2"
              onClick={() => {
                resetPwForm();
                setPwOpen(true);
              }}
            >
              <KeyRoundIcon className="size-4" /> Alterar senha
            </Button>
          )}
          <Button
            variant="ghost"
            className="w-full justify-start gap-2"
            onClick={() => void onSignOut()}
          >
            <LogOutIcon className="size-4" /> Sair
          </Button>
        </div>
      </div>

      <Dialog
        open={pwOpen}
        onOpenChange={(open) => {
          setPwOpen(open);
          if (!open) resetPwForm();
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Alterar senha</DialogTitle>
            <DialogDescription>
              {pwDone
                ? "Sua senha foi alterada."
                : "Digite sua senha atual e escolha uma nova."}
            </DialogDescription>
          </DialogHeader>

          {!pwDone && (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                void onSubmitPassword();
              }}
            >
              <Input
                type="password"
                autoComplete="current-password"
                placeholder="Senha atual"
                value={oldPw}
                onChange={(e) => setOldPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="Nova senha"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              <Input
                type="password"
                autoComplete="new-password"
                placeholder="Confirmar nova senha"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                disabled={pwBusy}
                required
              />
              {pwError !== null && (
                <div
                  role="alert"
                  className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
                >
                  {pwError}
                </div>
              )}
              <DialogFooter>
                <Button
                  type="submit"
                  disabled={
                    pwBusy || oldPw.length === 0 || newPw.length === 0 || confirmPw.length === 0
                  }
                >
                  {pwBusy ? "Alterando…" : "Alterar senha"}
                </Button>
              </DialogFooter>
            </form>
          )}

          {pwDone && (
            <DialogFooter>
              <Button onClick={() => setPwOpen(false)}>Concluído</Button>
            </DialogFooter>
          )}
        </DialogContent>
      </Dialog>
    </Section>
  );
}

function ArchivedSection() {
  // includeArchived:true is the only way to load archived rows; the
  // default sidebar query no longer surfaces them.
  const query = useConversations("", true);
  const archived = useMemo(
    () => (query.data?.pages ?? []).flatMap((p) => p.data).filter((c) => c.archived === true),
    [query.data],
  );

  return (
    <Section
      title="Sessões arquivadas"
      description="Sessões que você arquivou. Restaure uma para a barra lateral, ou exclua-a definitivamente."
    >
      {query.isLoading ? (
        <p className="text-sm text-muted-foreground">Carregando…</p>
      ) : archived.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nenhuma sessão arquivada.</p>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {archived.map((conv) => (
            <ArchivedRow key={conv.id} conversation={conv} />
          ))}
        </ul>
      )}
    </Section>
  );
}

/**
 * One archived-session row. Not clickable (archived sessions aren't a
 * navigation target here); the title + timestamp read as a record, and the
 * Delete / Unarchive controls reveal on hover (always visible on touch).
 */
function ArchivedRow({ conversation }: { conversation: Conversation }) {
  const archive = useArchiveConversation();
  const del = useStopAndDeleteConversation();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const label = conversationDisplayLabel(conversation);
  const busy = archive.isPending || del.isPending;

  return (
    <li
      data-testid="archived-row"
      className="group relative flex items-center gap-2 rounded-md px-3 py-2 hover:bg-muted"
    >
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium" title={label}>
          {label}
        </div>
        <div className="text-xs text-muted-foreground">
          {absoluteTime(conversation.updated_at * 1000)}
        </div>
      </div>
      {/* Actions reveal on hover (desktop) / always shown on touch. */}
      <div className="flex shrink-0 items-center gap-1 transition-opacity md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100">
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          aria-label="Excluir sessão"
          data-testid="delete-archived"
          disabled={busy}
          onClick={() => setDeleteOpen(true)}
        >
          <Trash2Icon className="size-4 text-destructive" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          // No background in light mode (ghost). Dark mode needs a fill so the
          // button reads against the dark row — borrow the secondary tokens
          // there only, without touching the text color.
          className="gap-1.5 dark:bg-secondary dark:hover:bg-secondary/80"
          data-testid="unarchive-conversation"
          disabled={busy}
          onClick={() => archive.mutate({ id: conversation.id, archived: false })}
        >
          <ArchiveRestoreIcon className="size-3.5" />
          Desarquivar
        </Button>
      </div>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Excluir sessão?</DialogTitle>
            <DialogDescription>
              <span className="font-medium break-all">{label}</span> e todo o seu histórico serão
              removidos. Esta ação não pode ser desfeita.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDeleteOpen(false)} disabled={del.isPending}>
              Cancelar
            </Button>
            <Button
              variant="destructive"
              disabled={del.isPending}
              onClick={() => {
                // Fire-and-forget: the row drops out once the conversations
                // cache refreshes after the delete settles.
                del.mutate({ id: conversation.id });
                setDeleteOpen(false);
              }}
            >
              Excluir
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  );
}
