import { useCallback, useEffect, useState } from "react";

import { PageScroll } from "@/components/PageScroll";
import { authenticatedFetch } from "@/lib/identity";
import { Link, useNavigate } from "@/lib/routing";

interface Session {
  id: string;
  title: string | null;
  agent_name: string | null;
  status: string | null;
  workspace: string | null;
  updated_at: number | null;
  archived: boolean;
}

function relTime(ts: number | null): string {
  if (!ts) return "";
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return "agora";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}min`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
}

function statusColor(status: string | null): string {
  const s = (status ?? "").toLowerCase();
  if (s.includes("fail") || s.includes("error")) return "text-red-400";
  if (s.includes("run") || s.includes("active")) return "text-amber-400";
  return "text-muted-foreground";
}

export function CodePage() {
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[] | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/sessions?limit=100");
      if (res.ok) {
        const data = (await res.json()) as { data: Session[] };
        setSessions(data.data.filter((s) => !s.archived));
      } else setSessions([]);
    } catch {
      setSessions([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <PageScroll>
      <div className="mx-auto flex max-w-4xl flex-col gap-5 px-6 py-8">
        <header className="flex items-end justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h1 className="text-xl font-semibold">Code</h1>
            <p className="text-sm opacity-60">Suas sessões de código com agentes.</p>
          </div>
          <Link
            to="/"
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-black"
            style={{ backgroundColor: "var(--brand-accent)" }}
          >
            Nova sessão →
          </Link>
        </header>

        {sessions === null ? (
          <p className="text-sm opacity-60">Carregando…</p>
        ) : sessions.length === 0 ? (
          <p className="text-sm opacity-40">
            Nenhuma sessão ainda —{" "}
            <Link to="/" className="underline">
              inicie uma no Início
            </Link>
            .
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {sessions.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => navigate(`/c/${s.id}`)}
                className="flex items-center justify-between gap-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-left transition hover:border-white/25 hover:bg-black/30"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-medium">
                      {s.title?.trim() || "Sessão sem título"}
                    </span>
                    {s.agent_name && (
                      <span className="shrink-0 rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70">
                        {s.agent_name}
                      </span>
                    )}
                  </div>
                  {s.workspace && (
                    <span className="mt-0.5 block truncate text-xs opacity-50">
                      📁 {s.workspace}
                    </span>
                  )}
                </div>
                <div className="flex shrink-0 flex-col items-end gap-0.5 text-xs">
                  <span className="opacity-50">{relTime(s.updated_at)}</span>
                  {s.status && <span className={statusColor(s.status)}>{s.status}</span>}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </PageScroll>
  );
}
