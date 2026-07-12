import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";

interface GalleryAgent {
  id: string;
  name: string;
  description: string;
  harness: string | null;
  subagents: number;
  subagent_names: string[];
  skills: string[];
  prompt_preview: string;
  installed: boolean;
}

function Chips({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs opacity-40">{label}</span>
      {items.map((it) => (
        <span key={it} className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-80">
          {it}
        </span>
      ))}
    </div>
  );
}

export function GalleryPage() {
  const navigate = useNavigate();
  const [agents, setAgents] = useState<GalleryAgent[] | null>(null);
  const [installing, setInstalling] = useState<string | null>(null);

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

  const install = async (a: GalleryAgent) => {
    setInstalling(a.id);
    try {
      const res = await authenticatedFetch(
        `/v1/gallery/agents/${encodeURIComponent(a.id)}/install`,
        {
          method: "POST",
        },
      );
      if (res.ok) await load();
    } finally {
      setInstalling(null);
    }
  };

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Galeria de agentes</h1>
        <p className="text-sm opacity-60">
          Agentes prontos que acompanham o OmniCraft. Instale um e ele aparece em <b>Nova sessão</b>
          .
        </p>
      </header>

      {agents === null ? (
        <p className="text-sm opacity-60">Carregando…</p>
      ) : agents.length === 0 ? (
        <p className="text-sm opacity-40">Nenhum agente de exemplo encontrado.</p>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {agents.map((a) => (
            <div
              key={a.id}
              className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h2 className="text-base font-semibold capitalize">{a.name}</h2>
                  {a.harness && (
                    <span className="mt-0.5 inline-block rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70">
                      {a.harness}
                    </span>
                  )}
                </div>
              </div>
              <p className="flex-1 text-sm leading-relaxed opacity-80">{a.description || "—"}</p>
              <div className="flex flex-col gap-1.5">
                <Chips label="🤝 sub-agentes" items={a.subagent_names} />
                <Chips label="🧩 skills" items={a.skills} />
              </div>
              <div className="flex items-center gap-2">
                {a.installed ? (
                  <>
                    <span
                      className="rounded-lg px-3 py-1.5 text-sm font-medium"
                      style={{ backgroundColor: "#30a46c22", color: "#30a46c" }}
                    >
                      ✓ Instalado
                    </span>
                    <button
                      type="button"
                      onClick={() => navigate("/code")}
                      className="rounded-lg border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
                    >
                      Nova sessão →
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    disabled={installing === a.id}
                    onClick={() => void install(a)}
                    className="rounded-lg px-4 py-1.5 text-sm font-medium text-black disabled:opacity-40"
                    style={{ backgroundColor: "var(--brand-accent)" }}
                  >
                    {installing === a.id ? "Instalando…" : "Instalar"}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
