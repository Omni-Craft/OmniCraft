import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { PageScroll } from "@/components/PageScroll";
import { authenticatedFetch } from "@/lib/identity";
import { Link, useNavigate } from "@/lib/routing";
import { HomeModeToggle, useCraftworkRoute } from "@/shell/craftworkNav";

const CostPage = lazy(() => import("@/pages/CostPage").then((m) => ({ default: m.CostPage })));
const EvalsPage = lazy(() => import("@/pages/EvalsPage").then((m) => ({ default: m.EvalsPage })));
const GalleryPage = lazy(() =>
  import("@/pages/GalleryPage").then((m) => ({ default: m.GalleryPage })),
);
const ScheduledAgentsPage = lazy(() =>
  import("@/pages/ScheduledAgentsPage").then((m) => ({ default: m.ScheduledAgentsPage })),
);

interface GalleryAgent {
  id: string;
  name: string;
  description: string;
  subagent_names: string[];
  skills: string[];
  installed: boolean;
}

const TOOLS: { to: string; emoji: string; title: string; desc: string }[] = [
  {
    to: "/craftwork/gallery",
    emoji: "🧩",
    title: "Galeria de agentes",
    desc: "Instale times prontos e inicie uma sessão.",
  },
  {
    to: "/craftwork/scheduled",
    emoji: "📅",
    title: "Agentes agendados",
    desc: "Dispare por horário, intervalo ou webhook.",
  },
  {
    to: "/arena",
    emoji: "⚔️",
    title: "Arena",
    desc: "Compare o mesmo prompt em vários harnesses.",
  },
  {
    to: "/craftwork/evals",
    emoji: "✅",
    title: "Avaliações",
    desc: "Suites de regressão para seus agentes.",
  },
  {
    to: "/craftwork/costs",
    emoji: "📊",
    title: "Custos",
    desc: "Uso e gasto por dia, modelo e sessão.",
  },
  {
    to: "/github",
    emoji: "🔗",
    title: "GitHub",
    desc: "Abra uma sessão a partir de uma issue ou PR.",
  },
];

function Hub() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<GalleryAgent[] | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/gallery/agents");
      if (res.ok) setAgents(((await res.json()) as { data: GalleryAgent[] }).data);
      else setAgents([]);
    } catch {
      setAgents([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const installed = (agents ?? []).filter((a) => a.installed);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-8 px-6 py-10">
      <header className="flex flex-col gap-2">
        <HomeModeToggle />
        <div className="flex items-center gap-2">
          <span className="text-2xl">🛠️</span>
          <h1 className="text-2xl font-semibold tracking-tight">Craftwork</h1>
        </div>
        <p className="max-w-2xl text-sm opacity-60">
          Seu espaço de trabalho com agentes. Inicie uma tarefa, agende execuções, compare modelos e
          componha times de especialistas — tudo num só lugar.
        </p>
      </header>

      {/* Tool cards */}
      <section className="flex flex-col gap-3">
        <h2 className="text-xs font-semibold uppercase tracking-wide opacity-50">Ferramentas</h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {TOOLS.map((t) => (
            <Link
              key={t.to}
              to={t.to}
              className="group flex flex-col gap-2 rounded-xl border border-border bg-card/40 p-4 transition hover:border-foreground/30 hover:bg-card/60"
            >
              <span className="text-2xl">{t.emoji}</span>
              <span className="font-semibold">{t.title}</span>
              <span className="text-sm opacity-60">{t.desc}</span>
            </Link>
          ))}
        </div>
      </section>

      {/* Installed agents as launch cards */}
      <section className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide opacity-50">Seus agentes</h2>
          <Link to="/craftwork/gallery" className="text-xs opacity-60 hover:opacity-100">
            Ver galeria →
          </Link>
        </div>
        {agents === null ? (
          <p className="text-sm opacity-50">Carregando…</p>
        ) : installed.length === 0 ? (
          <p className="text-sm opacity-40">
            Nenhum agente instalado ainda —{" "}
            <Link to="/craftwork/gallery" className="underline">
              instale um na galeria
            </Link>
            .
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {installed.map((a) => (
              <div
                key={a.id}
                className="flex flex-col gap-2 rounded-xl border border-border bg-card/40 p-4"
              >
                <span className="font-semibold capitalize">{a.name}</span>
                <span className="line-clamp-3 flex-1 text-sm opacity-60">
                  {a.description || "—"}
                </span>
                {(a.subagent_names.length > 0 || a.skills.length > 0) && (
                  <span className="text-[11px] opacity-40">
                    {a.subagent_names.length > 0 && `🤝 ${a.subagent_names.length} `}
                    {a.skills.length > 0 && `🧩 ${a.skills.length}`}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => navigate("/code")}
                  className="mt-1 self-start rounded-lg px-3 py-1.5 text-sm font-medium text-black"
                  style={{ backgroundColor: "var(--brand-accent)" }}
                >
                  Nova sessão →
                </button>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

/**
 * Craftwork content panel. The section nav lives in the sidebar
 * (CraftworkSidebarBody); this renders the selected section into the AppShell
 * outlet, mirroring SettingsPage. `home` is the Cowork-style hub.
 */
export function CraftworkPage() {
  const { section } = useCraftworkRoute();
  return (
    <PageScroll>
      <Suspense fallback={null}>
        {section === "home" && <Hub />}
        {section === "gallery" && <GalleryPage />}
        {section === "scheduled" && <ScheduledAgentsPage />}
        {section === "evals" && <EvalsPage />}
        {section === "costs" && <CostPage />}
      </Suspense>
    </PageScroll>
  );
}
