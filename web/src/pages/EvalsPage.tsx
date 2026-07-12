import { useCallback, useEffect, useMemo, useState } from "react";

import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { authenticatedFetch } from "@/lib/identity";
import { fetchLastAssistantText } from "@/lib/lastAssistantText";
import { nativeWrapperLabelsForAgent } from "@/lib/nativeCodingAgents";
import { useNavigate } from "@/lib/routing";

interface Check {
  type: "contains" | "not_contains" | "regex";
  value: string;
}
interface Task {
  id: string;
  prompt: string;
  check: Check;
}
interface Suite {
  id: string;
  name: string;
  created_at: number;
  tasks: Task[];
}
interface RunResult {
  task_id: string;
  prompt: string;
  passed: boolean;
  session_id: string | null;
  output: string;
}
interface Run {
  id: string;
  label: string;
  created_at: number;
  passed: number;
  total: number;
  results: RunResult[];
}

const CHECK_LABEL: Record<Check["type"], string> = {
  contains: "contém",
  not_contains: "não contém",
  regex: "regex",
};

const accent = { backgroundColor: "var(--brand-accent)" };
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ── Create-suite form ──────────────────────────────────────────────────────

function NewSuite({ onCreated }: { onCreated: (s: Suite) => void }) {
  const [name, setName] = useState("");
  const [tasks, setTasks] = useState<{ prompt: string; type: Check["type"]; value: string }[]>([
    { prompt: "", type: "contains", value: "" },
  ]);
  const [busy, setBusy] = useState(false);

  const setTask = (i: number, patch: Partial<(typeof tasks)[number]>) =>
    setTasks((prev) => prev.map((t, idx) => (idx === i ? { ...t, ...patch } : t)));

  const canSave = name.trim() && tasks.some((t) => t.prompt.trim());

  const save = async () => {
    setBusy(true);
    try {
      const res = await authenticatedFetch("/v1/evals/suites", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          tasks: tasks
            .filter((t) => t.prompt.trim())
            .map((t) => ({ prompt: t.prompt.trim(), check: { type: t.type, value: t.value } })),
        }),
      });
      if (res.ok) {
        onCreated((await res.json()) as Suite);
        setName("");
        setTasks([{ prompt: "", type: "contains", value: "" }]);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Nome da suíte (ex.: Regressão do agente de suporte)"
        className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/25"
      />
      {tasks.map((t, i) => (
        <div key={i} className="flex flex-col gap-2 rounded-lg border border-white/5 p-2">
          <textarea
            value={t.prompt}
            onChange={(e) => setTask(i, { prompt: e.target.value })}
            rows={2}
            placeholder="Prompt da tarefa…"
            className="resize-y rounded-md border border-white/10 bg-black/20 p-2 text-sm outline-none focus:border-white/25"
          />
          <div className="flex items-center gap-2">
            <span className="text-xs opacity-50">Aprovar se a saída</span>
            <select
              value={t.type}
              onChange={(e) => setTask(i, { type: e.target.value as Check["type"] })}
              className="rounded-md border border-white/10 bg-black/20 px-2 py-1 text-sm outline-none"
            >
              <option value="contains">contém</option>
              <option value="not_contains">não contém</option>
              <option value="regex">regex</option>
            </select>
            <input
              value={t.value}
              onChange={(e) => setTask(i, { value: e.target.value })}
              placeholder="valor esperado"
              className="min-w-0 flex-1 rounded-md border border-white/10 bg-black/20 px-2 py-1 text-sm outline-none focus:border-white/25"
            />
            {tasks.length > 1 && (
              <button
                type="button"
                onClick={() => setTasks((prev) => prev.filter((_, idx) => idx !== i))}
                className="rounded px-2 text-sm opacity-50 hover:opacity-100"
              >
                ×
              </button>
            )}
          </div>
        </div>
      ))}
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setTasks((prev) => [...prev, { prompt: "", type: "contains", value: "" }])}
          className="rounded-lg border border-white/15 px-3 py-1.5 text-sm hover:border-white/30"
        >
          + Tarefa
        </button>
        <button
          type="button"
          disabled={!canSave || busy}
          onClick={() => void save()}
          className="rounded-lg px-4 py-1.5 text-sm font-medium text-black disabled:opacity-40"
          style={accent}
        >
          {busy ? "Criando…" : "Criar suíte"}
        </button>
      </div>
    </div>
  );
}

// ── Suites list ────────────────────────────────────────────────────────────

function EvalsList({ onOpen }: { onOpen: (suiteId: string) => void }) {
  const [suites, setSuites] = useState<Suite[] | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    const res = await authenticatedFetch("/v1/evals/suites");
    if (res.ok) setSuites(((await res.json()) as { data: Suite[] }).data);
    else setSuites([]);
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-5 px-6 py-8">
      <header className="flex items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Avaliações de agentes</h1>
          <p className="text-sm opacity-60">
            Suítes de tarefas com verificação automática — rode e compare execuções pra pegar
            regressões.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating((v) => !v)}
          className="shrink-0 rounded-lg px-3 py-2 text-sm font-medium text-black"
          style={accent}
        >
          {creating ? "Fechar" : "Nova suíte"}
        </button>
      </header>

      {creating && (
        <NewSuite
          onCreated={(s) => {
            setCreating(false);
            void load();
            onOpen(s.id);
          }}
        />
      )}

      {suites === null ? (
        <p className="text-sm opacity-60">Carregando…</p>
      ) : suites.length === 0 ? (
        <p className="text-sm opacity-40">Nenhuma suíte ainda. Crie uma pra começar.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {suites.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                onClick={() => onOpen(s.id)}
                className="flex w-full items-center justify-between gap-3 rounded-xl border border-white/10 bg-black/20 px-4 py-3 text-left transition hover:border-white/25"
              >
                <span className="font-medium">{s.name}</span>
                <span className="text-xs opacity-50">{s.tasks.length} tarefas</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Suite detail: run + history/regression ──────────────────────────────────

function passRatioTone(passed: number, total: number): string {
  if (total === 0) return "#8b949e";
  const r = passed / total;
  return r === 1 ? "#30a46c" : r === 0 ? "#e5484d" : "#e3a008";
}

function SuiteDetail({ suiteId, onBack }: { suiteId: string; onBack: () => void }) {
  const navigate = useNavigate();
  const { data: agents } = useAvailableAgents();
  const { data: hosts } = useHosts();
  const [suite, setSuite] = useState<Suite | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [agentId, setAgentId] = useState("");
  const [hostId, setHostId] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [label, setLabel] = useState("");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState("");

  const onlineHosts = useMemo(
    () => (hosts ?? []).filter((h: Host) => h.status === "online"),
    [hosts],
  );

  const load = useCallback(async () => {
    const [sRes, rRes] = await Promise.all([
      authenticatedFetch("/v1/evals/suites"),
      authenticatedFetch(`/v1/evals/suites/${encodeURIComponent(suiteId)}/runs`),
    ]);
    if (sRes.ok) {
      const found = ((await sRes.json()) as { data: Suite[] }).data.find((s) => s.id === suiteId);
      setSuite(found ?? null);
    }
    if (rRes.ok) setRuns(((await rRes.json()) as { data: Run[] }).data);
  }, [suiteId]);
  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!agentId && agents && agents.length > 0) setAgentId(agents[0].id);
  }, [agents, agentId]);
  useEffect(() => {
    if (!hostId && onlineHosts.length === 1) setHostId(onlineHosts[0].host_id);
  }, [hostId, onlineHosts]);

  const runTask = async (prompt: string): Promise<{ sessionId: string; output: string }> => {
    const agent = (agents ?? []).find((a) => a.id === agentId);
    const res = await authenticatedFetch("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_id: agentId,
        host_id: hostId,
        workspace: workspace.trim(),
        labels: nativeWrapperLabelsForAgent(agent) ?? undefined,
        initial_items: [
          {
            type: "message",
            data: { role: "user", content: [{ type: "input_text", text: prompt }] },
          },
        ],
      }),
    });
    const sessionId = ((await res.json()) as { id: string }).id;
    const base = `/v1/sessions/${encodeURIComponent(sessionId)}`;
    // Poll until the session goes idle again (turn finished).
    for (let i = 0; i < 120; i++) {
      await sleep(2500);
      try {
        const s = await authenticatedFetch(base);
        if (s.ok) {
          const status = ((await s.json()) as { status?: string }).status;
          if (status === "idle" || status === "completed" || status === "failed") break;
        }
      } catch {
        /* keep polling */
      }
    }
    const output = (await fetchLastAssistantText(sessionId, 4000)) ?? "";
    return { sessionId, output };
  };

  const run = async () => {
    if (!suite || !agentId || !hostId || !workspace.trim().startsWith("/")) return;
    setRunning(true);
    try {
      const outputs: { task_id: string; session_id: string; output: string }[] = [];
      for (let i = 0; i < suite.tasks.length; i++) {
        setProgress(`Tarefa ${i + 1}/${suite.tasks.length}…`);
        const task = suite.tasks[i];
        const { sessionId, output } = await runTask(task.prompt);
        outputs.push({ task_id: task.id, session_id: sessionId, output });
      }
      setProgress("Avaliando…");
      await authenticatedFetch(`/v1/evals/suites/${encodeURIComponent(suiteId)}/runs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() || agentId, task_outputs: outputs }),
      });
      await load();
    } finally {
      setRunning(false);
      setProgress("");
    }
  };

  if (suite === null) {
    return (
      <div className="px-6 py-10 text-sm opacity-60">
        Suíte não encontrada.{" "}
        <button className="underline" onClick={onBack}>
          Voltar
        </button>
      </div>
    );
  }

  // Regression: a task that passed in the previous run but fails in the newest.
  const regressed = new Set<string>();
  if (runs.length >= 2) {
    const prev = new Map(runs[1].results.map((r) => [r.task_id, r.passed]));
    for (const r of runs[0].results) {
      if (prev.get(r.task_id) === true && r.passed === false) regressed.add(r.task_id);
    }
  }

  const workspaceValid = workspace.trim().startsWith("/");

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-5 px-6 py-8">
      <header className="flex items-center justify-between gap-3">
        <div>
          <button className="text-xs opacity-50 hover:opacity-100" onClick={onBack}>
            ← Avaliações
          </button>
          <h1 className="text-xl font-semibold">{suite.name}</h1>
        </div>
      </header>

      {/* Run config */}
      <div className="flex flex-col gap-3 rounded-xl border border-white/10 bg-black/20 p-4">
        <span className="text-sm font-medium">Rodar avaliação</span>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <select
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
            className="rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-sm outline-none"
          >
            {(agents ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.display_name || a.name}
              </option>
            ))}
          </select>
          <select
            value={hostId}
            onChange={(e) => setHostId(e.target.value)}
            className="rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-sm outline-none"
          >
            <option value="">
              {onlineHosts.length === 0 ? "Nenhuma máquina online" : "Escolha a máquina…"}
            </option>
            {onlineHosts.map((h: Host) => (
              <option key={h.host_id} value={h.host_id}>
                {h.name}
              </option>
            ))}
          </select>
          <input
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            placeholder="/diretório de trabalho"
            className="rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-sm outline-none focus:border-white/25"
          />
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Rótulo (ex.: versão/data)"
            className="rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-sm outline-none focus:border-white/25"
          />
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={running || !agentId || !hostId || !workspaceValid}
            onClick={() => void run()}
            className="rounded-lg px-4 py-2 text-sm font-medium text-black disabled:opacity-40"
            style={accent}
          >
            {running ? progress || "Rodando…" : `Rodar ${suite.tasks.length} tarefas`}
          </button>
          <span className="text-xs opacity-50">
            Cada tarefa roda numa sessão e a saída final é verificada.
          </span>
        </div>
      </div>

      {/* Tasks */}
      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium opacity-80">Tarefas</h2>
        {suite.tasks.map((t) => (
          <div
            key={t.id}
            className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm"
          >
            <p className="opacity-90">{t.prompt}</p>
            <p className="mt-0.5 text-xs opacity-50">
              aprovar se a saída {CHECK_LABEL[t.check.type]}{" "}
              <code className="rounded bg-white/10 px-1">{t.check.value || "—"}</code>
            </p>
          </div>
        ))}
      </section>

      {/* Runs / regression */}
      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-medium opacity-80">Execuções</h2>
        {runs.length === 0 ? (
          <p className="text-sm opacity-40">Nenhuma execução ainda.</p>
        ) : (
          <div className="flex flex-col gap-3">
            {regressed.size > 0 && (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                ⚠️ {regressed.size} tarefa(s) regrediram na última execução (passavam antes,
                falharam agora).
              </p>
            )}
            {runs.map((r, ri) => (
              <div key={r.id} className="rounded-xl border border-white/10 bg-black/20 p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-sm font-medium">{r.label}</span>
                  <span
                    className="rounded-md px-2 py-0.5 text-sm font-medium tabular-nums"
                    style={{
                      backgroundColor: `${passRatioTone(r.passed, r.total)}22`,
                      color: passRatioTone(r.passed, r.total),
                    }}
                  >
                    {r.passed}/{r.total} passou
                  </span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {r.results.map((res) => {
                    const isReg = ri === 0 && regressed.has(res.task_id);
                    return (
                      <button
                        key={res.task_id}
                        type="button"
                        title={res.prompt}
                        onClick={() => res.session_id && navigate(`/c/${res.session_id}`)}
                        className="rounded px-2 py-1 text-xs"
                        style={{
                          backgroundColor: res.passed ? "#30a46c22" : "#e5484d22",
                          color: res.passed ? "#30a46c" : "#e5484d",
                          outline: isReg ? "1px solid #e5484d" : undefined,
                        }}
                      >
                        {res.passed ? "✓" : "✗"} {res.prompt.slice(0, 24)}
                        {res.prompt.length > 24 ? "…" : ""}
                      </button>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

export function EvalsPage() {
  const [selected, setSelected] = useState<string | null>(null);
  return selected ? (
    <SuiteDetail suiteId={selected} onBack={() => setSelected(null)} />
  ) : (
    <EvalsList onOpen={setSelected} />
  );
}
