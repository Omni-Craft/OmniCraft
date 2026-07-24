import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { authenticatedFetch } from "@/lib/identity";
import { cn } from "@/lib/utils";
import { useNavigate } from "@/lib/routing";
import { CheckIcon, SearchIcon } from "lucide-react";

interface GalleryAgent {
  id: string;
  name: string;
  description: string;
  category: string;
  harness: string | null;
  subagents: number;
  subagent_names: string[];
  skills: string[];
  prompt_preview: string;
  installed: boolean;
}

// Filter-tab label for a category (singular → plural heading). Unknown
// categories fall back to a capitalized form so a new config `category:` still
// gets a readable tab without a code change.
const CATEGORY_TAB: Record<string, string> = {
  orquestrador: "Orquestradores",
  fábrica: "Fábricas",
  conversa: "Conversa",
};
// Display order for the category tabs; anything else trails alphabetically.
const CATEGORY_ORDER = ["orquestrador", "fábrica", "conversa"];

function tabLabel(category: string): string {
  if (!category) return "Outros";
  return CATEGORY_TAB[category] ?? category.charAt(0).toUpperCase() + category.slice(1);
}

// Stable avatar tint per agent — a hash of the id picks from a fixed palette so
// a given agent always wears the same color across reloads.
const AVATAR_COLORS = [
  "#39c9b4",
  "#e8a83c",
  "#a884f0",
  "#4a90e2",
  "#e86a9c",
  "#3ec6e0",
  "#5cbf6a",
  "#f0776a",
];

function avatarColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function initial(name: string): string {
  const m = name.match(/[a-z0-9]/i);
  return (m ? m[0] : "?").toUpperCase();
}

// The footer chips: lead with sub-agents when present, else skills, capped so a
// card never grows tall. A "+N" pill folds whatever didn't fit.
const MAX_CHIPS = 3;

function chipData(a: GalleryAgent): { label: string; chips: string[]; more: string | null } {
  const hasSub = a.subagent_names.length > 0;
  const primary = hasSub ? a.subagent_names : a.skills;
  const chips = primary.slice(0, MAX_CHIPS);
  const extraPrimary = primary.length - chips.length;
  const foldedSkills = hasSub ? a.skills.length : 0;
  const remaining = extraPrimary + foldedSkills;
  const noun = foldedSkills > 0 ? "skills" : "sub-agentes";
  return {
    label: hasSub ? "sub-agentes" : "skills",
    chips,
    more: remaining > 0 ? `+${remaining} ${noun}` : null,
  };
}

function Chip({ children }: { children: React.ReactNode }) {
  return (
    <span className="rounded-md border border-border/60 bg-muted px-2 py-0.5 text-[11.5px] text-muted-foreground">
      {children}
    </span>
  );
}

export function GalleryPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<GalleryAgent[] | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("todos");
  const [query, setQuery] = useState("");
  const [details, setDetails] = useState<GalleryAgent | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/gallery/agents");
      if (res.ok) {
        const data = ((await res.json()) as { data: GalleryAgent[] }).data;
        // Tolerate a backend that predates the `category` field (version skew
        // during a rollout) — everything without one lands in a catch-all tab.
        setAgents(data.map((a) => ({ ...a, category: a.category || "outros" })));
      } else setAgents([]);
    } catch {
      setAgents([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  // ⌘K / Ctrl-K focuses the search, matching the hint in the box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        document.getElementById("gallery-search")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const install = async (a: GalleryAgent) => {
    setInstalling(a.id);
    try {
      const res = await authenticatedFetch(
        `/v1/gallery/agents/${encodeURIComponent(a.id)}/install`,
        { method: "POST" },
      );
      if (res.ok) await load();
    } finally {
      setInstalling(null);
    }
  };

  const categories = useMemo(() => {
    const seen = [...new Set((agents ?? []).map((a) => a.category))];
    return seen.sort((x, y) => {
      const ix = CATEGORY_ORDER.indexOf(x);
      const iy = CATEGORY_ORDER.indexOf(y);
      if (ix !== -1 || iy !== -1) return (ix === -1 ? 99 : ix) - (iy === -1 ? 99 : iy);
      return x.localeCompare(y);
    });
  }, [agents]);

  const installedCount = (agents ?? []).filter((a) => a.installed).length;

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (agents ?? []).filter((a) => {
      const passFilter =
        filter === "todos" ? true : filter === "instalados" ? a.installed : a.category === filter;
      const passQuery =
        !q ||
        a.name.toLowerCase().includes(q) ||
        a.description.toLowerCase().includes(q) ||
        a.category.toLowerCase().includes(q) ||
        a.subagent_names.some((s) => s.toLowerCase().includes(q)) ||
        a.skills.some((s) => s.toLowerCase().includes(q));
      return passFilter && passQuery;
    });
  }, [agents, filter, query]);

  const tabs: { key: string; label: string; count: number | null }[] = [
    { key: "todos", label: "Todos", count: agents?.length ?? 0 },
    { key: "instalados", label: "Instalados", count: installedCount },
    ...categories.map((c) => ({ key: c, label: tabLabel(c), count: null })),
  ];

  return (
    <div className="mx-auto flex max-w-5xl flex-col px-6 py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">Galeria de agentes</h1>
          <p className="max-w-[52ch] text-sm text-muted-foreground">
            Agentes prontos que acompanham o OmniCraft. Instale um e ele aparece em{" "}
            <b className="font-medium text-foreground">Nova sessão</b>.
          </p>
        </div>
        <div className="relative w-full sm:w-64">
          <SearchIcon className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            id="gallery-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Buscar agentes"
            aria-label="Buscar agentes"
            className="pl-9"
          />
          <kbd className="pointer-events-none absolute top-1/2 right-2.5 -translate-y-1/2 rounded border border-border px-1.5 text-[10px] text-muted-foreground">
            ⌘K
          </kbd>
        </div>
      </header>

      {agents !== null && agents.length > 0 && (
        <div className="mt-5 flex flex-wrap gap-2">
          {tabs.map((t) => {
            const on = filter === t.key;
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => setFilter(t.key)}
                className={cn(
                  "rounded-full border px-3.5 py-1.5 text-xs font-medium transition-colors",
                  on
                    ? "border-transparent bg-[color-mix(in_srgb,var(--brand-accent)_16%,transparent)] text-foreground"
                    : "border-border text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
                {t.count !== null && <span className="ml-1 opacity-60">· {t.count}</span>}
              </button>
            );
          })}
        </div>
      )}

      <div className="mt-5">
        {agents === null ? (
          <p className="text-sm text-muted-foreground">Carregando…</p>
        ) : agents.length === 0 ? (
          <p className="text-sm text-muted-foreground opacity-70">
            Nenhum agente de exemplo encontrado.
          </p>
        ) : visible.length === 0 ? (
          <p className="text-sm text-muted-foreground opacity-70">
            Nenhum agente encontrado para esse filtro.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {visible.map((a) => {
              const c = chipData(a);
              return (
                <article
                  key={a.id}
                  className="flex flex-col rounded-xl border border-border bg-card p-[18px] transition-colors hover:border-foreground/20"
                >
                  <div className="flex items-start gap-3">
                    <div
                      className="grid size-10 shrink-0 place-items-center rounded-[11px] text-[15px] font-bold text-black/85"
                      style={{ backgroundColor: avatarColor(a.id) }}
                      aria-hidden
                    >
                      {initial(a.name)}
                    </div>
                    <div className="min-w-0 flex-1">
                      <h2 className="truncate text-[15px] font-semibold capitalize">{a.name}</h2>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        omnicraft · {a.category}
                      </p>
                    </div>
                    {a.installed && (
                      <Badge
                        variant="outline"
                        className="shrink-0 gap-1 border-success/30 bg-success/10 text-success"
                      >
                        <CheckIcon className="size-3" />
                        Instalado
                      </Badge>
                    )}
                  </div>

                  <p className="mt-3 line-clamp-4 flex-1 text-[13px] leading-relaxed text-muted-foreground">
                    {a.description || "—"}
                  </p>

                  {c.chips.length > 0 && (
                    <div className="mt-4 flex flex-wrap items-center gap-1.5">
                      <span className="text-[11.5px] text-muted-foreground opacity-70">
                        {c.label}
                      </span>
                      {c.chips.map((chip) => (
                        <Chip key={chip}>{chip}</Chip>
                      ))}
                      {c.more && (
                        <span className="text-[11.5px] font-medium text-[color-mix(in_srgb,var(--brand-accent)_85%,var(--foreground))]">
                          {c.more}
                        </span>
                      )}
                    </div>
                  )}

                  <div className="mt-4 flex items-center gap-2 border-t border-border pt-4">
                    {a.installed ? (
                      <Button
                        onClick={() => navigate("/code")}
                        className="text-black"
                        style={{ backgroundColor: "var(--brand-accent)" }}
                      >
                        Nova sessão →
                      </Button>
                    ) : (
                      <Button
                        disabled={installing === a.id}
                        onClick={() => void install(a)}
                        className="text-black"
                        style={{ backgroundColor: "var(--brand-accent)" }}
                      >
                        {installing === a.id ? "Instalando…" : "Instalar"}
                      </Button>
                    )}
                    <Button variant="outline" onClick={() => setDetails(a)}>
                      Detalhes
                    </Button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>

      <Dialog open={details !== null} onOpenChange={(o) => !o && setDetails(null)}>
        <DialogContent className="max-w-lg">
          {details && (
            <>
              <DialogHeader>
                <div className="flex items-center gap-3">
                  <div
                    className="grid size-10 shrink-0 place-items-center rounded-[11px] text-[15px] font-bold text-black/85"
                    style={{ backgroundColor: avatarColor(details.id) }}
                    aria-hidden
                  >
                    {initial(details.name)}
                  </div>
                  <div className="min-w-0">
                    <DialogTitle className="capitalize">{details.name}</DialogTitle>
                    <DialogDescription>
                      omnicraft · {details.category}
                      {details.harness ? ` · ${details.harness}` : ""}
                    </DialogDescription>
                  </div>
                </div>
              </DialogHeader>

              <div className="flex flex-col gap-4">
                <p className="text-sm leading-relaxed text-muted-foreground">
                  {details.description || "—"}
                </p>

                {details.subagent_names.length > 0 && (
                  <section className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-muted-foreground">
                      Sub-agentes · {details.subagent_names.length}
                    </span>
                    <div className="flex flex-wrap gap-1.5">
                      {details.subagent_names.map((s) => (
                        <Chip key={s}>{s}</Chip>
                      ))}
                    </div>
                  </section>
                )}

                {details.skills.length > 0 && (
                  <section className="flex flex-col gap-1.5">
                    <span className="text-xs font-medium text-muted-foreground">
                      Skills · {details.skills.length}
                    </span>
                    <div className="flex max-h-40 flex-wrap gap-1.5 overflow-y-auto">
                      {details.skills.map((s) => (
                        <Chip key={s}>{s}</Chip>
                      ))}
                    </div>
                  </section>
                )}

                <div className="flex items-center gap-2 border-t border-border pt-4">
                  {details.installed ? (
                    <Button
                      onClick={() => navigate("/code")}
                      className="text-black"
                      style={{ backgroundColor: "var(--brand-accent)" }}
                    >
                      Nova sessão →
                    </Button>
                  ) : (
                    <Button
                      disabled={installing === details.id}
                      onClick={() => void install(details)}
                      className="text-black"
                      style={{ backgroundColor: "var(--brand-accent)" }}
                    >
                      {installing === details.id ? "Instalando…" : "Instalar"}
                    </Button>
                  )}
                </div>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
