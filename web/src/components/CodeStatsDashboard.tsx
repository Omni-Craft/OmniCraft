import { useCallback, useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { cn } from "@/lib/utils";

interface Stats {
  total_sessions: number;
  total_tokens: number;
  total_usd: number;
  active_days: number;
  current_streak: number;
  longest_streak: number;
  peak_hour: number | null;
  favorite_model: string | null;
  by_model: { model: string; total_tokens: number; usd: number }[];
  daily: Record<string, number>;
  window_days: number;
}

const WINDOWS: { label: string; days: number }[] = [
  { label: "Todos", days: 365 },
  { label: "30d", days: 30 },
  { label: "7d", days: 7 },
];

// War and Peace ≈ 587k words ≈ ~780k tokens — the classic "big number" yardstick.
const WAR_AND_PEACE_TOKENS = 780_000;

const nf = new Intl.NumberFormat("pt-BR");
const compact = new Intl.NumberFormat("pt-BR", { notation: "compact", maximumFractionDigits: 1 });

function fmtTokens(n: number): string {
  return n >= 10_000 ? compact.format(n) : nf.format(n);
}

function heatColor(count: number): string {
  if (count <= 0) return "bg-foreground/5";
  if (count === 1) return "bg-brand-accent/30";
  if (count <= 3) return "bg-brand-accent/55";
  if (count <= 6) return "bg-brand-accent/80";
  return "bg-brand-accent";
}

function dayKey(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate(),
  ).padStart(2, "0")}`;
}

/** Build week-columns (7 rows) ending today, oldest week first. */
function buildWeeks(daily: Record<string, number>, days: number): number[][] {
  const span = Math.min(Math.max(days, 7), 182);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  // Start on the Sunday on/before the window start so columns align to weeks.
  const start = new Date(today);
  start.setDate(start.getDate() - span + 1);
  start.setDate(start.getDate() - start.getDay());
  const weeks: number[][] = [];
  const cur = new Date(start);
  while (cur <= today) {
    const col: number[] = [];
    for (let i = 0; i < 7; i++) {
      col.push(cur > today ? -1 : (daily[dayKey(cur)] ?? 0));
      cur.setDate(cur.getDate() + 1);
    }
    weeks.push(col);
  }
  return weeks;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-lg bg-foreground/[0.03] px-3 py-2.5">
      <span className="text-[11px] text-muted-foreground">{label}</span>
      <span className="font-semibold text-lg tabular-nums leading-none">{value}</span>
    </div>
  );
}

export function CodeStatsDashboard() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [tab, setTab] = useState<"overview" | "models">("overview");
  const [windowDays, setWindowDays] = useState(365);

  const load = useCallback(async (days: number) => {
    try {
      const res = await authenticatedFetch(`/v1/code-stats?days=${days}`);
      if (res.ok) setStats((await res.json()) as Stats);
    } catch {
      /* ignore */
    }
  }, []);
  useEffect(() => {
    void load(windowDays);
  }, [load, windowDays]);

  const weeks = useMemo(() => (stats ? buildWeeks(stats.daily, stats.window_days) : []), [stats]);
  const maxModelTokens = useMemo(
    () => Math.max(1, ...(stats?.by_model.map((m) => m.total_tokens) ?? [1])),
    [stats],
  );

  if (!stats) return null;

  const wp = stats.total_tokens / WAR_AND_PEACE_TOKENS;
  const wpLine =
    wp >= 1
      ? `Você usou ~${wp < 10 ? wp.toFixed(1) : Math.round(wp)}× mais tokens que Guerra e Paz.`
      : `Você usou ~${Math.round(wp * 100)}% dos tokens de Guerra e Paz.`;

  return (
    <div className="w-full rounded-xl border border-border bg-card/40 p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-1 text-sm">
          {(["overview", "models"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => setTab(t)}
              className={cn(
                "rounded-md px-2.5 py-1 font-medium transition-colors",
                tab === t
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {t === "overview" ? "Visão Geral" : "Modelos"}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w.days}
              type="button"
              onClick={() => setWindowDays(w.days)}
              className={cn(
                "rounded-md px-2 py-1 text-xs transition-colors",
                windowDays === w.days
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {w.label}
            </button>
          ))}
        </div>
      </div>

      {tab === "overview" ? (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Sessões" value={nf.format(stats.total_sessions)} />
            <Stat label="Total de tokens" value={fmtTokens(stats.total_tokens)} />
            <Stat label="Dias ativos" value={nf.format(stats.active_days)} />
            <Stat label="Gasto (USD)" value={`$${stats.total_usd.toFixed(2)}`} />
            <Stat label="Sequência atual" value={`${stats.current_streak}d`} />
            <Stat label="Maior sequência" value={`${stats.longest_streak}d`} />
            <Stat
              label="Horário de pico"
              value={stats.peak_hour == null ? "—" : `${stats.peak_hour}h`}
            />
            <Stat label="Modelo favorito" value={stats.favorite_model ?? "—"} />
          </div>

          {/* Activity heatmap */}
          <div className="mt-3 overflow-x-auto">
            <div className="flex gap-[3px]">
              {weeks.map((col, ci) => (
                <div key={ci} className="flex flex-col gap-[3px]">
                  {col.map((count, ri) => (
                    <div
                      key={ri}
                      className={cn(
                        "size-2.5 rounded-[2px]",
                        count < 0 ? "bg-transparent" : heatColor(count),
                      )}
                      title={count >= 0 ? `${count} sessão(ões)` : ""}
                    />
                  ))}
                </div>
              ))}
            </div>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">{wpLine}</p>
        </>
      ) : (
        <div className="flex flex-col gap-2">
          {stats.by_model.length === 0 ? (
            <p className="text-sm text-muted-foreground">Sem uso registrado ainda.</p>
          ) : (
            stats.by_model.map((m) => (
              <div key={m.model} className="flex flex-col gap-1">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium">{m.model}</span>
                  <span className="text-muted-foreground tabular-nums">
                    {fmtTokens(m.total_tokens)} tok · ${m.usd.toFixed(2)}
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-foreground/10">
                  <div
                    className="h-full rounded-full bg-brand-accent"
                    style={{ width: `${Math.max(3, (m.total_tokens / maxModelTokens) * 100)}%` }}
                  />
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
