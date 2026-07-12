import { useCallback, useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";

interface HistoryEntry {
  at: number;
  trigger: string;
  status: string;
  detail?: string;
  session_id?: string | null;
}

interface Job {
  id: string;
  name: string;
  agent_name: string;
  prompt: string;
  workspace: string | null;
  host_id: string | null;
  interval_seconds: number | null;
  enabled: boolean;
  webhook_token: string;
  created_at: number;
  last_run_at: number | null;
  next_run_at: number | null;
  history: HistoryEntry[];
}

const UNITS: { label: string; seconds: number }[] = [
  { label: "minutos", seconds: 60 },
  { label: "horas", seconds: 3600 },
  { label: "dias", seconds: 86400 },
];

function humanInterval(seconds: number | null): string {
  if (!seconds) return "";
  for (const u of [...UNITS].reverse()) {
    if (seconds % u.seconds === 0) return `${seconds / u.seconds} ${u.label}`;
  }
  return `${seconds}s`;
}

function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("pt-BR");
}

interface FormState {
  name: string;
  agent_name: string;
  prompt: string;
  workspace: string;
  mode: "schedule" | "webhook";
  every: number;
  unit: number; // seconds per unit
}

const EMPTY_FORM: FormState = {
  name: "",
  agent_name: "",
  prompt: "",
  workspace: "",
  mode: "schedule",
  every: 1,
  unit: 3600,
};

export function ScheduledAgentsPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fireResult, setFireResult] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/scheduled-agents");
      if (res.ok) {
        const data = (await res.json()) as { data: Job[]; agents: string[] };
        setJobs(data.data);
        setAgents(data.agents);
      } else setJobs([]);
    } catch {
      setJobs([]);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  const intervalSeconds = form.mode === "schedule" ? Math.max(1, form.every) * form.unit : null;

  const resetForm = () => {
    setForm({ ...EMPTY_FORM, agent_name: agents[0] ?? "" });
    setEditingId(null);
    setError(null);
  };

  useEffect(() => {
    if (!form.agent_name && agents.length) setForm((f) => ({ ...f, agent_name: agents[0] }));
  }, [agents, form.agent_name]);

  const submit = async () => {
    setError(null);
    if (!form.agent_name) return setError("Escolha um agente.");
    if (!form.prompt.trim()) return setError("O prompt é obrigatório.");
    if (!form.workspace.trim())
      return setError("O workspace é obrigatório (caminho absoluto onde o agente roda).");
    setBusy(true);
    try {
      const payload = {
        name: form.name.trim() || form.agent_name,
        agent_name: form.agent_name,
        prompt: form.prompt,
        workspace: form.workspace.trim(),
        interval_seconds: intervalSeconds,
        enabled: true,
      };
      const res = editingId
        ? await authenticatedFetch(`/v1/scheduled-agents/${editingId}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          })
        : await authenticatedFetch("/v1/scheduled-agents", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as {
          error?: { message?: string };
        } | null;
        setError(body?.error?.message ?? `Falha (HTTP ${res.status}).`);
        return;
      }
      resetForm();
      await load();
    } finally {
      setBusy(false);
    }
  };

  const beginEdit = (j: Job) => {
    const unit =
      j.interval_seconds && j.interval_seconds % 86400 === 0
        ? 86400
        : j.interval_seconds && j.interval_seconds % 3600 === 0
          ? 3600
          : 60;
    setForm({
      name: j.name,
      agent_name: j.agent_name,
      prompt: j.prompt,
      workspace: j.workspace ?? "",
      mode: j.interval_seconds ? "schedule" : "webhook",
      every: j.interval_seconds ? j.interval_seconds / unit : 1,
      unit,
    });
    setEditingId(j.id);
    setError(null);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const toggle = async (j: Job) => {
    await authenticatedFetch(`/v1/scheduled-agents/${j.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !j.enabled }),
    });
    await load();
  };

  const remove = async (j: Job) => {
    if (!window.confirm(`Excluir o agendamento "${j.name}"?`)) return;
    await authenticatedFetch(`/v1/scheduled-agents/${j.id}`, { method: "DELETE" });
    await load();
  };

  const runNow = async (j: Job) => {
    setFireResult((r) => ({ ...r, [j.id]: "Disparando…" }));
    try {
      const res = await authenticatedFetch(`/v1/scheduled-agents/${j.id}/run`, { method: "POST" });
      const body = (await res.json().catch(() => null)) as {
        status?: string;
        detail?: string;
        session_id?: string;
      } | null;
      setFireResult((r) => ({
        ...r,
        [j.id]:
          body?.status === "started"
            ? "✓ Sessão iniciada"
            : `${body?.status}: ${body?.detail ?? ""}`,
      }));
      await load();
    } catch {
      setFireResult((r) => ({ ...r, [j.id]: "Falha ao disparar" }));
    }
  };

  const webhookUrl = (token: string) => `${window.location.origin}/v1/webhooks/${token}`;
  const copy = (text: string) => void navigator.clipboard?.writeText(text);

  const inputCls =
    "w-full rounded-lg border border-white/15 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/30";

  const agentOptions = useMemo(
    () => (agents.length ? agents : form.agent_name ? [form.agent_name] : []),
    [agents, form.agent_name],
  );

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Agentes agendados</h1>
        <p className="text-sm opacity-60">
          Dispare um agente automaticamente — em intervalos ou por um webhook (URL pública). Cada
          disparo abre uma sessão real que você pode acompanhar.
        </p>
      </header>

      {/* Create / edit form */}
      <section className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4">
        <h2 className="text-sm font-semibold opacity-80">
          {editingId ? "Editar agendamento" : "Novo agendamento"}
        </h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Nome
            <input
              className={inputCls}
              value={form.name}
              placeholder="Ex.: Resumo diário"
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Agente
            <select
              className={inputCls}
              value={form.agent_name}
              onChange={(e) => setForm({ ...form, agent_name: e.target.value })}
            >
              {agentOptions.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs opacity-70">
          Prompt (primeira mensagem)
          <textarea
            className={`${inputCls} min-h-[72px] resize-y`}
            value={form.prompt}
            placeholder="O que o agente deve fazer quando disparado?"
            onChange={(e) => setForm({ ...form, prompt: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs opacity-70">
          Workspace (caminho absoluto no host)
          <input
            className={inputCls}
            value={form.workspace}
            placeholder="/Users/voce/projeto"
            onChange={(e) => setForm({ ...form, workspace: e.target.value })}
          />
        </label>
        <div className="flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1 text-xs opacity-70">
            Gatilho
            <div className="flex gap-2">
              {(["schedule", "webhook"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setForm({ ...form, mode: m })}
                  className={`rounded-lg border px-3 py-1.5 text-sm transition ${
                    form.mode === m
                      ? "border-white/40 bg-white/10"
                      : "border-white/15 hover:border-white/30"
                  }`}
                >
                  {m === "schedule" ? "🕒 Agendado" : "🔗 Webhook"}
                </button>
              ))}
            </div>
          </div>
          {form.mode === "schedule" && (
            <div className="flex flex-col gap-1 text-xs opacity-70">
              A cada
              <div className="flex gap-2">
                <input
                  type="number"
                  min={1}
                  className={`${inputCls} w-20`}
                  value={form.every}
                  onChange={(e) => setForm({ ...form, every: Number(e.target.value) || 1 })}
                />
                <select
                  className={inputCls}
                  value={form.unit}
                  onChange={(e) => setForm({ ...form, unit: Number(e.target.value) })}
                >
                  {UNITS.map((u) => (
                    <option key={u.seconds} value={u.seconds}>
                      {u.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
        </div>
        {error && <p className="text-sm text-red-400">{error}</p>}
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void submit()}
            className="rounded-lg px-4 py-1.5 text-sm font-medium text-black disabled:opacity-40"
            style={{ backgroundColor: "var(--brand-accent)" }}
          >
            {busy ? "Salvando…" : editingId ? "Salvar" : "Criar agendamento"}
          </button>
          {editingId && (
            <button
              type="button"
              onClick={resetForm}
              className="rounded-lg border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
            >
              Cancelar
            </button>
          )}
        </div>
      </section>

      {/* Jobs list */}
      {jobs === null ? (
        <p className="text-sm opacity-60">Carregando…</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm opacity-40">Nenhum agendamento ainda.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {jobs.map((j) => (
            <div
              key={j.id}
              className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <h3 className="text-base font-semibold">{j.name}</h3>
                    <span className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70">
                      {j.agent_name}
                    </span>
                    {j.interval_seconds ? (
                      <span className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70">
                        🕒 a cada {humanInterval(j.interval_seconds)}
                      </span>
                    ) : (
                      <span className="rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70">
                        🔗 webhook
                      </span>
                    )}
                  </div>
                  <p className="mt-1 line-clamp-2 text-sm opacity-70">{j.prompt}</p>
                </div>
                <label className="flex shrink-0 items-center gap-1.5 text-xs opacity-70">
                  <input type="checkbox" checked={j.enabled} onChange={() => void toggle(j)} />
                  Ativo
                </label>
              </div>

              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs opacity-60">
                <span>📁 {j.workspace ?? "—"}</span>
                {j.interval_seconds && <span>Próximo: {fmtTime(j.next_run_at)}</span>}
                <span>Último: {fmtTime(j.last_run_at)}</span>
              </div>

              {/* Webhook URL */}
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded bg-black/40 px-2 py-1 text-[11px] opacity-70">
                  {webhookUrl(j.webhook_token)}
                </code>
                <button
                  type="button"
                  onClick={() => copy(webhookUrl(j.webhook_token))}
                  className="rounded border border-white/15 px-2 py-1 text-[11px] transition hover:border-white/30"
                >
                  Copiar
                </button>
              </div>

              {/* History */}
              {j.history.length > 0 && (
                <div className="flex flex-col gap-0.5 text-[11px] opacity-55">
                  {j.history.slice(0, 3).map((h, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <span>{fmtTime(h.at)}</span>
                      <span className="opacity-70">{h.trigger}</span>
                      <span
                        className={
                          h.status === "started"
                            ? "text-emerald-400"
                            : h.status === "skipped"
                              ? "text-amber-400"
                              : "text-red-400"
                        }
                      >
                        {h.status}
                      </span>
                      {h.session_id && (
                        <button
                          type="button"
                          onClick={() => navigate(`/c/${h.session_id}`)}
                          className="underline decoration-dotted hover:opacity-100"
                        >
                          abrir sessão
                        </button>
                      )}
                      {!h.session_id && h.detail && <span className="opacity-60">{h.detail}</span>}
                    </div>
                  ))}
                </div>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => void runNow(j)}
                  className="rounded-lg px-3 py-1.5 text-sm font-medium text-black"
                  style={{ backgroundColor: "var(--brand-accent)" }}
                >
                  Executar agora
                </button>
                <button
                  type="button"
                  onClick={() => beginEdit(j)}
                  className="rounded-lg border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
                >
                  Editar
                </button>
                <button
                  type="button"
                  onClick={() => void remove(j)}
                  className="rounded-lg border border-white/15 px-3 py-1.5 text-sm text-red-400 transition hover:border-red-400/50"
                >
                  Excluir
                </button>
                {fireResult[j.id] && <span className="text-xs opacity-70">{fireResult[j.id]}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
