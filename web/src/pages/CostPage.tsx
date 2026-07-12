import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";

const REFRESH_MS = 5000;
const BUDGET_KEY = "omnicraft.costs.dailyBudgetUsd";

interface ModelCost {
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  usd: number;
}
interface SessionCost {
  id: string;
  title: string;
  usd: number;
  tokens: number;
}
interface CostData {
  today_usd: number;
  total_usd: number;
  total_tokens: number;
  session_count: number;
  daily: { day: string; usd: number }[];
  by_model: ModelCost[];
  top_sessions: SessionCost[];
}

const usd = (n: number): string =>
  n >= 100 ? `$${n.toFixed(0)}` : n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(3)}`;

function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** A short model tone so the same model reads consistently across the panel. */
const MODEL_COLORS = ["#0fb5bd", "#7c5cff", "#e3742a", "#30a46c", "#e5484d", "#e3a008", "#3b82f6"];
function modelColor(index: number): string {
  return MODEL_COLORS[index % MODEL_COLORS.length];
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-white/10 bg-black/20 px-4 py-3">
      <span className="text-xs opacity-50">{label}</span>
      <span
        className="text-2xl font-semibold tabular-nums"
        style={tone ? { color: tone } : undefined}
      >
        {value}
      </span>
    </div>
  );
}

export function CostPage() {
  const navigate = useNavigate();
  const [data, setData] = useState<CostData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [budget, setBudget] = useState<string>(() => {
    try {
      return localStorage.getItem(BUDGET_KEY) ?? "";
    } catch {
      return "";
    }
  });

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/observability/costs?days=30");
      if (!res.ok) {
        setError(`Erro ${res.status}`);
        return;
      }
      setData((await res.json()) as CostData);
      setError(null);
    } catch {
      setError("Falha ao carregar os custos.");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const onBudget = (v: string) => {
    setBudget(v);
    try {
      if (v.trim()) localStorage.setItem(BUDGET_KEY, v.trim());
      else localStorage.removeItem(BUDGET_KEY);
    } catch {
      /* ignore */
    }
  };

  if (error && data === null) {
    return <div className="px-6 py-10 text-sm text-red-400">{error}</div>;
  }
  if (data === null) {
    return <div className="px-6 py-10 text-sm opacity-60">Carregando custos…</div>;
  }

  const budgetNum = Number(budget);
  const hasBudget = budget.trim() !== "" && !Number.isNaN(budgetNum) && budgetNum > 0;
  const overBudget = hasBudget && data.today_usd > budgetNum;
  const budgetPct = hasBudget ? Math.min(100, (data.today_usd / budgetNum) * 100) : 0;

  const maxDaily = Math.max(1e-9, ...data.daily.map((d) => d.usd));
  const maxModel = Math.max(1e-9, ...data.by_model.map((m) => m.usd));

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Custos & observabilidade</h1>
        <p className="text-sm opacity-60">Gasto de LLM ao vivo — atualiza a cada 5s.</p>
      </header>

      {/* Top stats */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Hoje" value={usd(data.today_usd)} tone={overBudget ? "#e5484d" : undefined} />
        <Stat label="Total (histórico)" value={usd(data.total_usd)} />
        <Stat label="Tokens (total)" value={tokens(data.total_tokens)} />
        <Stat label="Sessões com custo" value={String(data.session_count)} />
      </div>

      {/* Daily budget + alert */}
      <div className="flex flex-col gap-2 rounded-xl border border-white/10 bg-black/20 px-4 py-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-sm">
            <span className="opacity-70">Orçamento diário (alerta)</span>
            <span className="opacity-40">$</span>
            <input
              value={budget}
              onChange={(e) => onBudget(e.target.value)}
              inputMode="decimal"
              placeholder="—"
              className="w-24 rounded-md border border-white/10 bg-transparent px-2 py-1 text-sm tabular-nums outline-none focus:border-white/30"
            />
          </label>
          {hasBudget && (
            <span
              className="text-sm font-medium tabular-nums"
              style={{ color: overBudget ? "#e5484d" : "#30a46c" }}
            >
              {usd(data.today_usd)} / {usd(budgetNum)} {overBudget ? "· estourou!" : ""}
            </span>
          )}
        </div>
        {hasBudget && (
          <div className="h-2 overflow-hidden rounded-full bg-white/10">
            <div
              className="h-full rounded-full transition-all"
              style={{
                width: `${budgetPct}%`,
                backgroundColor: overBudget ? "#e5484d" : "#0fb5bd",
              }}
            />
          </div>
        )}
      </div>

      {/* Daily trend */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-medium opacity-80">Últimos 30 dias</h2>
        <div className="flex h-32 items-end gap-1 rounded-xl border border-white/10 bg-black/20 px-3 py-3">
          {data.daily.map((d) => (
            <div
              key={d.day}
              className="flex h-full flex-1 items-end"
              title={`${d.day}: ${usd(d.usd)}`}
            >
              <div
                className="w-full rounded-sm transition-all"
                style={{
                  height: `${Math.max(2, (d.usd / maxDaily) * 100)}%`,
                  backgroundColor: d.usd > 0 ? "var(--brand-accent)" : "rgba(255,255,255,0.08)",
                }}
              />
            </div>
          ))}
        </div>
      </section>

      {/* By model */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-medium opacity-80">Por modelo</h2>
        {data.by_model.length === 0 ? (
          <p className="text-sm opacity-40">Sem custo registrado ainda.</p>
        ) : (
          <div className="flex flex-col gap-2 rounded-xl border border-white/10 bg-black/20 p-3">
            {data.by_model.map((m, i) => (
              <div key={m.model} className="flex items-center gap-3">
                <span className="w-40 shrink-0 truncate text-sm" title={m.model}>
                  {m.model}
                </span>
                <div className="h-4 flex-1 overflow-hidden rounded bg-white/5">
                  <div
                    className="h-full rounded"
                    style={{
                      width: `${(m.usd / maxModel) * 100}%`,
                      backgroundColor: modelColor(i),
                    }}
                  />
                </div>
                <span className="w-16 shrink-0 text-right text-sm tabular-nums">{usd(m.usd)}</span>
                <span className="w-16 shrink-0 text-right text-xs tabular-nums opacity-50">
                  {tokens(m.total_tokens)}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Top sessions */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-medium opacity-80">Sessões mais caras</h2>
        {data.top_sessions.length === 0 ? (
          <p className="text-sm opacity-40">Nenhuma sessão com custo.</p>
        ) : (
          <div className="divide-y divide-white/5 rounded-xl border border-white/10 bg-black/20">
            {data.top_sessions.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => navigate(`/c/${s.id}`)}
                className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition hover:bg-white/5"
              >
                <span className="min-w-0 truncate text-sm">{s.title}</span>
                <span className="flex shrink-0 items-center gap-3 tabular-nums">
                  <span className="text-xs opacity-50">{tokens(s.tokens)} tok</span>
                  <span className="text-sm font-medium">{usd(s.usd)}</span>
                </span>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
