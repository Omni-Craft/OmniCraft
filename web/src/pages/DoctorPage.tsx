import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { cn } from "@/lib/utils";

interface Check {
  id: string;
  label: string;
  ok: boolean;
  detail: string;
  hint: string | null;
}

interface Report {
  checks: Check[];
  ok: number;
  total: number;
}

/** Environment checklist — every "não funciona" the server can self-diagnose. */
export function DoctorPage() {
  const [report, setReport] = useState<Report | "loading" | "error">("loading");

  const load = useCallback(async () => {
    setReport("loading");
    try {
      const res = await authenticatedFetch("/v1/doctor");
      if (!res.ok) {
        setReport("error");
        return;
      }
      setReport((await res.json()) as Report);
    } catch {
      setReport("error");
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5 px-6 py-8">
      <header className="flex items-end justify-between gap-3">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">Diagnóstico</h1>
          <p className="text-sm opacity-60">
            Verificação do ambiente: máquinas, agentes, integrações e agendamentos.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded-lg border border-border px-3 py-1.5 text-sm transition hover:border-foreground/30"
        >
          Verificar de novo
        </button>
      </header>

      {report === "loading" ? (
        <p className="text-sm opacity-60">Verificando…</p>
      ) : report === "error" ? (
        <p className="text-sm text-destructive">Falha ao consultar o diagnóstico.</p>
      ) : (
        <>
          <p className="text-sm opacity-70">
            {report.ok === report.total
              ? "✅ Tudo certo — nenhum problema encontrado."
              : `${report.ok}/${report.total} verificações OK.`}
          </p>
          <div className="flex flex-col gap-2">
            {report.checks.map((c) => (
              <div
                key={c.id}
                className={cn(
                  "flex flex-col gap-1 rounded-xl border p-3",
                  c.ok ? "border-border bg-card/40" : "border-amber-500/40 bg-amber-500/10",
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="text-base leading-none">{c.ok ? "✅" : "⚠️"}</span>
                  <span className="font-medium text-sm">{c.label}</span>
                  <span className="ml-auto text-xs opacity-60">{c.detail}</span>
                </div>
                {c.hint && <p className="pl-7 text-xs opacity-70">{c.hint}</p>}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
