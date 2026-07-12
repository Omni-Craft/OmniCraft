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
  cron: string | null;
  tz: string | null;
  no_overlap: boolean;
  enabled: boolean;
  webhook_token: string;
  last_run_at: number | null;
  last_session_id: string | null;
  next_run_at: number | null;
  history: HistoryEntry[];
}

interface HostInfo {
  host_id: string;
  name: string;
  status: string;
}

type Mode = "interval" | "cron" | "webhook";

const UNITS: { label: string; seconds: number }[] = [
  { label: "minutos", seconds: 60 },
  { label: "horas", seconds: 3600 },
  { label: "dias", seconds: 86400 },
];

const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Todo dia 9h", cron: "0 9 * * *" },
  { label: "Dias úteis 9h", cron: "0 9 * * 1-5" },
  { label: "Toda segunda 9h", cron: "0 9 * * 1" },
  { label: "A cada hora", cron: "0 * * * *" },
];

const BROWSER_TZ =
  (typeof Intl !== "undefined" && Intl.DateTimeFormat().resolvedOptions().timeZone) || "UTC";

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

function statusColor(status: string): string {
  const s = status.toLowerCase();
  if (s.includes("fail") || s.includes("error")) return "text-red-400";
  if (s.includes("run") || s.includes("active")) return "text-amber-400";
  return "text-emerald-400";
}

function statusLabel(status: string, active: boolean): string {
  if (active) return "rodando";
  const s = status.toLowerCase();
  if (s.includes("fail") || s.includes("error")) return "falhou";
  if (s.includes("run") || s.includes("active")) return "rodando";
  return "concluída";
}

interface FormState {
  name: string;
  agent_name: string;
  prompt: string;
  workspace: string;
  host_id: string;
  mode: Mode;
  every: number;
  unit: number;
  cron: string;
  tz: string;
  no_overlap: boolean;
}

const EMPTY_FORM: FormState = {
  name: "",
  agent_name: "",
  prompt: "",
  workspace: "",
  host_id: "",
  mode: "interval",
  every: 1,
  unit: 3600,
  cron: "0 9 * * 1-5",
  tz: BROWSER_TZ,
  no_overlap: true,
};

export function ScheduledAgentsPage() {
  const navigate = useNavigate();
  const [jobs, setJobs] = useState<Job[] | null>(null);
  const [agents, setAgents] = useState<string[]>([]);
  const [hosts, setHosts] = useState<HostInfo[]>([]);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fireResult, setFireResult] = useState<Record<string, string>>({});
  const [testOpen, setTestOpen] = useState<Record<string, string>>({});
  const [sessionStatus, setSessionStatus] = useState<
    Record<string, { status: string; active: boolean }>
  >({});

  const fetchStatuses = useCallback(async (list: Job[]) => {
    const ids = list.map((j) => j.last_session_id).filter((x): x is string => !!x);
    await Promise.all(
      ids.map(async (id) => {
        try {
          const res = await authenticatedFetch(`/v1/sessions/${id}`);
          if (!res.ok) return;
          const d = (await res.json()) as { status?: string; active_response_id?: string | null };
          setSessionStatus((m) => ({
            ...m,
            [id]: { status: d.status ?? "", active: d.active_response_id != null },
          }));
        } catch {
          /* ignore */
        }
      }),
    );
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch("/v1/scheduled-agents");
      if (res.ok) {
        const data = (await res.json()) as { data: Job[]; agents: string[] };
        setJobs(data.data);
        setAgents(data.agents);
        void fetchStatuses(data.data);
      } else setJobs([]);
    } catch {
      setJobs([]);
    }
  }, [fetchStatuses]);

  useEffect(() => {
    void load();
    void (async () => {
      try {
        const res = await authenticatedFetch("/v1/hosts");
        if (res.ok) {
          const d = (await res.json()) as { hosts: HostInfo[] };
          setHosts(d.hosts.filter((h) => h.status === "online"));
        }
      } catch {
        /* ignore */
      }
    })();
  }, [load]);

  useEffect(() => {
    if (!form.agent_name && agents.length) setForm((f) => ({ ...f, agent_name: agents[0] }));
  }, [agents, form.agent_name]);

  const resetForm = () => {
    setForm({ ...EMPTY_FORM, agent_name: agents[0] ?? "" });
    setEditingId(null);
    setError(null);
  };

  const buildSchedule = (f: FormState) => {
    if (f.mode === "interval")
      return { interval_seconds: Math.max(1, f.every) * f.unit, cron: null, tz: null };
    if (f.mode === "cron")
      return { interval_seconds: null, cron: f.cron.trim(), tz: f.tz.trim() || null };
    return { interval_seconds: null, cron: null, tz: null };
  };

  const submit = async () => {
    setError(null);
    if (!form.agent_name) return setError("Escolha um agente.");
    if (!form.prompt.trim()) return setError("O prompt é obrigatório.");
    if (!form.workspace.trim())
      return setError("O workspace é obrigatório (caminho absoluto onde o agente roda).");
    if (form.mode === "cron" && !form.cron.trim()) return setError("Informe a expressão cron.");
    setBusy(true);
    try {
      const payload = {
        name: form.name.trim() || form.agent_name,
        agent_name: form.agent_name,
        prompt: form.prompt,
        workspace: form.workspace.trim(),
        host_id: form.host_id || null,
        no_overlap: form.no_overlap,
        enabled: true,
        ...buildSchedule(form),
      };
      const res = await authenticatedFetch(
        editingId ? `/v1/scheduled-agents/${editingId}` : "/v1/scheduled-agents",
        {
          method: editingId ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
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
      host_id: j.host_id ?? "",
      mode: j.cron ? "cron" : j.interval_seconds ? "interval" : "webhook",
      every: j.interval_seconds ? j.interval_seconds / unit : 1,
      unit,
      cron: j.cron ?? "0 9 * * 1-5",
      tz: j.tz ?? BROWSER_TZ,
      no_overlap: j.no_overlap,
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

  const fire = async (j: Job, payload?: unknown) => {
    setFireResult((r) => ({ ...r, [j.id]: "Disparando…" }));
    try {
      const res = await authenticatedFetch(`/v1/scheduled-agents/${j.id}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload !== undefined ? { payload } : {}),
      });
      const body = (await res.json().catch(() => null)) as {
        status?: string;
        detail?: string;
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

  const sendTest = async (j: Job) => {
    const raw = testOpen[j.id] ?? "";
    let payload: unknown = {};
    if (raw.trim()) {
      try {
        payload = JSON.parse(raw);
      } catch {
        setFireResult((r) => ({ ...r, [j.id]: "JSON de teste inválido" }));
        return;
      }
    }
    await fire(j, payload);
  };

  const webhookUrl = (token: string) => `${window.location.origin}/v1/webhooks/${token}`;
  const copy = (text: string) => void navigator.clipboard?.writeText(text);

  const inputCls =
    "w-full rounded-lg border border-white/15 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/30";
  const chip = "rounded bg-white/10 px-1.5 py-0.5 text-[11px] opacity-70";

  const agentOptions = useMemo(
    () => (agents.length ? agents : form.agent_name ? [form.agent_name] : []),
    [agents, form.agent_name],
  );

  const cadenceLabel = (j: Job) =>
    j.cron
      ? `🕒 ${j.cron}${j.tz ? ` (${j.tz})` : ""}`
      : j.interval_seconds
        ? `🕒 a cada ${humanInterval(j.interval_seconds)}`
        : "🔗 webhook";

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Agentes agendados</h1>
        <p className="text-sm opacity-60">
          Dispare um agente por intervalo, num horário (cron) ou por um webhook. O corpo do webhook
          é interpolado no prompt via <code className="opacity-80">{"{{campo}}"}</code>. Cada
          disparo abre uma sessão real que você pode acompanhar.
        </p>
      </header>

      {/* Create / edit form */}
      <section
        className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4"
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") void submit();
        }}
      >
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
          Prompt (primeira mensagem) — use {"{{campo}}"} para dados do webhook
          <textarea
            className={`${inputCls} min-h-[72px] resize-y`}
            value={form.prompt}
            placeholder="O que o agente deve fazer? Ex.: Triagem da issue {{issue.title}}"
            onChange={(e) => setForm({ ...form, prompt: e.target.value })}
          />
        </label>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Workspace (caminho absoluto no host)
            <input
              className={inputCls}
              value={form.workspace}
              placeholder="/Users/voce/projeto"
              onChange={(e) => setForm({ ...form, workspace: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Host
            <select
              className={inputCls}
              value={form.host_id}
              onChange={(e) => setForm({ ...form, host_id: e.target.value })}
            >
              <option value="">Automático (primeiro online)</option>
              {hosts.map((h) => (
                <option key={h.host_id} value={h.host_id}>
                  {h.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {/* Schedule */}
        <div className="flex flex-col gap-2">
          <div className="flex flex-col gap-1 text-xs opacity-70">
            Agendamento
            <div className="flex flex-wrap gap-2">
              {(
                [
                  ["interval", "🕒 Intervalo"],
                  ["cron", "📅 Horário (cron)"],
                  ["webhook", "🔗 Só webhook"],
                ] as const
              ).map(([m, label]) => (
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
                  {label}
                </button>
              ))}
            </div>
          </div>
          {form.mode === "interval" && (
            <div className="flex items-end gap-2 text-xs opacity-70">
              A cada
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
          )}
          {form.mode === "cron" && (
            <div className="flex flex-col gap-2">
              <div className="flex flex-wrap gap-1.5">
                {CRON_PRESETS.map((p) => (
                  <button
                    key={p.cron}
                    type="button"
                    onClick={() => setForm({ ...form, cron: p.cron })}
                    className="rounded border border-white/15 px-2 py-1 text-[11px] transition hover:border-white/30"
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                <label className="flex flex-col gap-1 text-xs opacity-70">
                  Expressão cron (min hora dia mês dia-semana)
                  <input
                    className={`${inputCls} font-mono`}
                    value={form.cron}
                    placeholder="0 9 * * 1-5"
                    onChange={(e) => setForm({ ...form, cron: e.target.value })}
                  />
                </label>
                <label className="flex flex-col gap-1 text-xs opacity-70">
                  Fuso horário
                  <input
                    className={inputCls}
                    value={form.tz}
                    placeholder="America/Sao_Paulo"
                    onChange={(e) => setForm({ ...form, tz: e.target.value })}
                  />
                </label>
              </div>
            </div>
          )}
          <label className="flex items-center gap-2 text-xs opacity-70">
            <input
              type="checkbox"
              checked={form.no_overlap}
              onChange={(e) => setForm({ ...form, no_overlap: e.target.checked })}
            />
            Não sobrepor — pular disparo agendado se a execução anterior ainda estiver rodando
          </label>
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
          <span className="text-[11px] opacity-40">⌘/Ctrl+Enter</span>
        </div>
      </section>

      {/* Jobs list */}
      {jobs === null ? (
        <p className="text-sm opacity-60">Carregando…</p>
      ) : jobs.length === 0 ? (
        <p className="text-sm opacity-40">Nenhum agendamento ainda.</p>
      ) : (
        <div className="flex flex-col gap-4">
          {jobs.map((j) => {
            const st = j.last_session_id ? sessionStatus[j.last_session_id] : undefined;
            return (
              <div
                key={j.id}
                className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-base font-semibold">{j.name}</h3>
                      <span className={chip}>{j.agent_name}</span>
                      <span className={chip}>{cadenceLabel(j)}</span>
                      {j.no_overlap && j.interval_seconds ? (
                        <span className={chip}>sem sobreposição</span>
                      ) : null}
                      {st && (
                        <span className={`text-[11px] ${statusColor(st.status)}`}>
                          última: {statusLabel(st.status, st.active)}
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
                  {(j.interval_seconds || j.cron) && <span>Próximo: {fmtTime(j.next_run_at)}</span>}
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
                        <span className={statusColor(h.status)}>{h.status}</span>
                        {h.session_id ? (
                          <button
                            type="button"
                            onClick={() => navigate(`/c/${h.session_id}`)}
                            className="underline decoration-dotted hover:opacity-100"
                          >
                            abrir sessão
                          </button>
                        ) : (
                          h.detail && <span className="opacity-60">{h.detail}</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Test payload */}
                {j.id in testOpen && (
                  <div className="flex flex-col gap-1.5">
                    <textarea
                      className={`${inputCls} min-h-[60px] resize-y font-mono text-[11px]`}
                      value={testOpen[j.id]}
                      placeholder='{"issue": {"title": "Bug de exemplo"}}'
                      onChange={(e) => setTestOpen((t) => ({ ...t, [j.id]: e.target.value }))}
                    />
                    <div className="flex gap-2">
                      <button
                        type="button"
                        onClick={() => void sendTest(j)}
                        className="rounded-lg px-3 py-1.5 text-sm font-medium text-black"
                        style={{ backgroundColor: "var(--brand-accent)" }}
                      >
                        Enviar teste
                      </button>
                      <button
                        type="button"
                        onClick={() =>
                          setTestOpen((t) => {
                            const n = { ...t };
                            delete n[j.id];
                            return n;
                          })
                        }
                        className="rounded-lg border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
                      >
                        Fechar
                      </button>
                    </div>
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => void fire(j)}
                    className="rounded-lg px-3 py-1.5 text-sm font-medium text-black"
                    style={{ backgroundColor: "var(--brand-accent)" }}
                  >
                    Executar agora
                  </button>
                  <button
                    type="button"
                    onClick={() => setTestOpen((t) => (j.id in t ? t : { ...t, [j.id]: "" }))}
                    className="rounded-lg border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
                  >
                    Testar webhook
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
                  {fireResult[j.id] && (
                    <span className="text-xs opacity-70">{fireResult[j.id]}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
