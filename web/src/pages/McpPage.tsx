import { useCallback, useEffect, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";
import { Link } from "@/lib/routing";

interface McpServer {
  name: string;
  transport: "http" | "stdio";
  description: string | null;
  url: string | null;
  command: string | null;
  args: string[];
}

interface AgentRow {
  id: string;
  name: string;
  display_name?: string;
}

interface FormState {
  name: string;
  transport: "http" | "stdio";
  description: string;
  url: string;
  command: string;
  args: string;
}

const EMPTY_FORM: FormState = {
  name: "",
  transport: "stdio",
  description: "",
  url: "",
  command: "",
  args: "",
};

function splitArgs(raw: string): string[] {
  return raw
    .split(/\s+/)
    .map((a) => a.trim())
    .filter(Boolean);
}

export function McpPage() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [agentId, setAgentId] = useState<string>("");
  const [servers, setServers] = useState<McpServer[] | "loading" | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, string>>({});

  useEffect(() => {
    void authenticatedFetch("/v1/agents")
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((j: { data: AgentRow[] }) => {
        setAgents(j.data);
        // Default to the chat agent — the most natural MCP consumer.
        const chat = j.data.find((a) => a.name === "chat");
        setAgentId((prev) => prev || chat?.id || j.data[0]?.id || "");
      })
      .catch(() => setAgents([]));
  }, []);

  const load = useCallback(async (id: string) => {
    if (!id) return;
    setServers("loading");
    setTestResult({});
    try {
      const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(id)}/mcp-servers`);
      setServers(res.ok ? ((await res.json()) as { data: McpServer[] }).data : []);
    } catch {
      setServers([]);
    }
  }, []);
  useEffect(() => {
    void load(agentId);
  }, [agentId, load]);

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditing(null);
    setError(null);
  };

  const submit = async () => {
    setError(null);
    if (!form.name.trim()) return setError("Nome é obrigatório.");
    if (form.transport === "http" && !/^https?:\/\//.test(form.url.trim()))
      return setError("URL http(s) é obrigatória para transporte HTTP.");
    if (form.transport === "stdio" && !form.command.trim())
      return setError("Comando é obrigatório para transporte stdio.");
    setBusy(true);
    try {
      const payload = {
        name: form.name.trim(),
        transport: form.transport,
        description: form.description.trim() || null,
        url: form.transport === "http" ? form.url.trim() : null,
        command: form.transport === "stdio" ? form.command.trim() : null,
        args: form.transport === "stdio" ? splitArgs(form.args) : [],
      };
      const res = await authenticatedFetch(
        editing
          ? `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers/${encodeURIComponent(editing)}`
          : `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers`,
        {
          method: editing ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) {
        const j = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
        setError(j?.error?.message ?? `Falha (HTTP ${res.status}).`);
        return;
      }
      resetForm();
      await load(agentId);
    } catch {
      setError("Falha de rede.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (name: string) => {
    if (!window.confirm(`Remover o servidor MCP "${name}" deste agente?`)) return;
    const res = await authenticatedFetch(
      `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    if (!res.ok) setError(`Falha ao remover (HTTP ${res.status}).`);
    await load(agentId);
  };

  const test = async (name: string) => {
    setTestResult((r) => ({ ...r, [name]: "Conectando…" }));
    try {
      const res = await authenticatedFetch(
        `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers/${encodeURIComponent(name)}/test`,
        { method: "POST" },
      );
      const j = (await res.json()) as {
        ok: boolean;
        tool_count?: number;
        tools?: string[];
        error?: string;
      };
      setTestResult((r) => ({
        ...r,
        [name]: j.ok
          ? `✅ ${j.tool_count} tools: ${(j.tools ?? []).slice(0, 5).join(", ")}${(j.tool_count ?? 0) > 5 ? "…" : ""}`
          : `❌ ${j.error}`,
      }));
    } catch {
      setTestResult((r) => ({ ...r, [name]: "❌ falha de rede" }));
    }
  };

  const beginEdit = (s: McpServer) => {
    setForm({
      name: s.name,
      transport: s.transport,
      description: s.description ?? "",
      url: s.url ?? "",
      command: s.command ?? "",
      args: (s.args ?? []).join(" "),
    });
    setEditing(s.name);
    setError(null);
  };

  const inputCls =
    "w-full rounded-lg border border-border bg-card/40 px-3 py-2 text-sm outline-none focus:border-ring";

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Servidores MCP</h1>
        <p className="text-sm opacity-60">
          Conecte ferramentas externas (Model Context Protocol) aos seus agentes — as tools do
          servidor ficam disponíveis nas sessões do agente. Segredos (env/headers) são preservados
          no bundle e nunca expostos aqui.
        </p>
      </header>

      {/* Agent picker */}
      <label className="flex max-w-md flex-col gap-1 text-xs opacity-70">
        Agente
        <select className={inputCls} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.display_name ?? a.name}
            </option>
          ))}
        </select>
      </label>

      {/* Server list */}
      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide opacity-50">
          Servidores do agente
        </h2>
        {servers === "loading" || servers === null ? (
          <p className="text-sm opacity-50">Carregando…</p>
        ) : servers.length === 0 ? (
          <p className="text-sm opacity-40">
            Nenhum servidor MCP neste agente — adicione um abaixo ou instale pelo diretório.
          </p>
        ) : (
          servers.map((s) => (
            <div
              key={s.name}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card/40 p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm font-medium">{s.name}</span>
                <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] opacity-70">
                  {s.transport}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs opacity-50">
                  {s.transport === "http" ? s.url : `${s.command} ${(s.args ?? []).join(" ")}`}
                </span>
                <button
                  type="button"
                  onClick={() => void test(s.name)}
                  className="rounded-lg bg-brand-accent/15 px-2.5 py-1 text-xs text-brand-accent transition hover:bg-brand-accent/25"
                  data-testid={`mcp-test-${s.name}`}
                >
                  Testar
                </button>
                <button
                  type="button"
                  onClick={() => beginEdit(s)}
                  className="rounded-lg border border-border px-2.5 py-1 text-xs transition hover:border-foreground/30"
                >
                  Editar
                </button>
                <button
                  type="button"
                  onClick={() => void remove(s.name)}
                  className="rounded-lg border border-border px-2.5 py-1 text-xs text-destructive transition hover:border-destructive/50"
                >
                  Remover
                </button>
              </div>
              {s.description && <p className="text-xs opacity-60">{s.description}</p>}
              {testResult[s.name] && (
                <p
                  className="font-mono text-xs opacity-80"
                  data-testid={`mcp-test-result-${s.name}`}
                >
                  {testResult[s.name]}
                </p>
              )}
            </div>
          ))
        )}
      </section>

      {/* Add / edit form */}
      <section className="flex flex-col gap-3 rounded-xl border border-border bg-card/40 p-4">
        <h2 className="text-sm font-semibold opacity-80">
          {editing ? `Editar "${editing}"` : "Adicionar servidor"}
        </h2>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Nome
            <input
              className={inputCls}
              value={form.name}
              placeholder="ex.: fetch"
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs opacity-70">
            Transporte
            <select
              className={inputCls}
              value={form.transport}
              onChange={(e) =>
                setForm({ ...form, transport: e.target.value as FormState["transport"] })
              }
            >
              <option value="stdio">stdio (comando local)</option>
              <option value="http">http (URL remota)</option>
            </select>
          </label>
        </div>
        {form.transport === "http" ? (
          <label className="flex flex-col gap-1 text-xs opacity-70">
            URL
            <input
              className={inputCls}
              value={form.url}
              placeholder="https://mcp.exemplo.com/sse"
              onChange={(e) => setForm({ ...form, url: e.target.value })}
            />
          </label>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="flex flex-col gap-1 text-xs opacity-70">
              Comando
              <input
                className={inputCls}
                value={form.command}
                placeholder="npx"
                onChange={(e) => setForm({ ...form, command: e.target.value })}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs opacity-70">
              Argumentos (separados por espaço)
              <input
                className={inputCls}
                value={form.args}
                placeholder="-y @modelcontextprotocol/server-fetch"
                onChange={(e) => setForm({ ...form, args: e.target.value })}
              />
            </label>
          </div>
        )}
        <label className="flex flex-col gap-1 text-xs opacity-70">
          Descrição (opcional)
          <input
            className={inputCls}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy || !agentId}
            onClick={() => void submit()}
            className="rounded-lg px-4 py-1.5 text-sm font-medium text-black disabled:opacity-40"
            style={{ backgroundColor: "var(--brand-accent)" }}
            data-testid="mcp-submit"
          >
            {busy ? "Salvando…" : editing ? "Salvar" : "Adicionar"}
          </button>
          {editing && (
            <button
              type="button"
              onClick={resetForm}
              className="rounded-lg border border-border px-3 py-1.5 text-sm transition hover:border-foreground/30"
            >
              Cancelar
            </button>
          )}
          <span className="text-[11px] opacity-40">
            Mudanças valem para novas sessões do agente.
          </span>
        </div>
      </section>

      {/* Discovery lives in the connector directory now — this page stays for
          manual and advanced setup (custom servers, editing, testing). */}
      <p className="text-sm opacity-60">
        Procurando um conector pronto?{" "}
        <Link to="/craftwork/connectors" className="underline underline-offset-2">
          Veja o diretório
        </Link>{" "}
        — instala num clique e já pede a credencial quando o conector precisa.
      </p>
    </div>
  );
}
