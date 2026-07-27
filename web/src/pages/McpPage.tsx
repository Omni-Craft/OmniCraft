import { useCallback, useEffect, useMemo, useState } from "react";

import { apiErrorMessage } from "@/lib/apiErrors";
import { avatarColor, avatarInitials } from "@/lib/avatarColor";
import { authenticatedFetch } from "@/lib/identity";
import { Link, useLocation } from "@/lib/routing";

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

/** A catalog entry, as the connector directory serves it. */
interface Connector {
  id: string;
  title: string;
  description: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  url?: string;
  env_required?: { name: string }[];
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

// The quick-fill shelf, in the order it reads best — the everyday servers
// first. Anything not listed (or that needs a credential) lives in the
// directory, which can collect the secret; this form cannot.
const SHELF = ["memory", "fetch", "filesystem", "playwright", "sequential-thinking", "git"];

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
  const [catalog, setCatalog] = useState<Connector[]>([]);
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
    void authenticatedFetch("/v1/mcp-catalog")
      .then((r) => (r.ok ? r.json() : { connectors: [] }))
      .then((j: { connectors?: Connector[] }) => setCatalog(j.connectors ?? []))
      .catch(() => setCatalog([]));
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

  // Only servers this form can fully describe: one that wants an API key would
  // save without it and fail at connect time, so those stay in the directory.
  const shelf = useMemo(() => {
    const byId = new Map(catalog.map((c) => [c.id, c]));
    return SHELF.map((id) => byId.get(id)).filter(
      (c): c is Connector => !!c && !(c.env_required ?? []).length,
    );
  }, [catalog]);

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setEditing(null);
    setError(null);
  };

  const prefill = (c: Connector) => {
    setForm({
      name: c.id,
      transport: c.transport,
      description: c.description,
      url: c.url ?? "",
      command: c.command ?? "",
      args: (c.args ?? []).join(" "),
    });
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
        setError(apiErrorMessage(j, `Falha (HTTP ${res.status}).`));
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
    "w-full rounded-lg border border-border bg-background/60 px-3 py-2 text-sm outline-none transition-colors placeholder:opacity-40 focus:border-ring";
  const labelCls = "flex flex-col gap-1.5 text-xs font-medium";
  const count = Array.isArray(servers) ? servers.length : null;
  const agent = agents.find((a) => a.id === agentId);
  // This page renders in both shells, and each has its own copy of the
  // directory. Crossing over would swap the sidebar out from under the reader,
  // so link to the one that lives in the shell they're already in.
  const inSettings = useLocation().pathname.includes("/settings");
  const directoryPath = inSettings ? "/settings/connectors" : "/craftwork/connectors";

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-7 px-6 py-8">
      <header className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">Servidores MCP</h1>
          <p className="max-w-[62ch] text-sm text-muted-foreground">
            Conecte ferramentas externas (Model Context Protocol) aos seus agentes. Segredos
            (env/headers) ficam no bundle e nunca são expostos aqui.
          </p>
        </div>
        <label className="flex w-full flex-col gap-1.5 text-xs font-medium sm:w-64">
          Agente
          <div className="relative">
            {agent && (
              <span
                className="pointer-events-none absolute top-1/2 left-2 grid size-6 -translate-y-1/2 place-items-center rounded-md text-[11px] font-bold text-black/85"
                style={{ backgroundColor: avatarColor(agent.id) }}
                aria-hidden
              >
                {avatarInitials(agent.display_name ?? agent.name, 2)}
              </span>
            )}
            <select
              className={`${inputCls} ${agent ? "pl-10" : ""}`}
              value={agentId}
              onChange={(e) => setAgentId(e.target.value)}
              aria-label="Agente"
            >
              {agents.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.display_name ?? a.name}
                </option>
              ))}
            </select>
          </div>
        </label>
      </header>

      {/* Server list */}
      <section className="flex flex-col gap-3">
        <div className="flex items-center gap-2">
          <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            Servidores do agente
          </h2>
          {count !== null && (
            <span className="rounded-full border border-border px-2 text-[11px] text-muted-foreground">
              {count}
            </span>
          )}
          <span className="h-px flex-1 bg-border" />
        </div>

        {servers === "loading" || servers === null ? (
          <p className="text-sm text-muted-foreground">Carregando…</p>
        ) : servers.length === 0 ? (
          <div className="flex items-center gap-3 rounded-xl border border-border bg-card/30 p-4">
            <span
              className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-sm text-muted-foreground"
              aria-hidden
            >
              ⇅
            </span>
            <p className="text-sm text-muted-foreground">
              Nenhum servidor MCP neste agente — adicione um abaixo ou use um item do catálogo.
            </p>
          </div>
        ) : (
          servers.map((s) => (
            <div
              key={s.name}
              className="flex flex-col gap-2 rounded-xl border border-border bg-card/40 p-3"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-mono text-sm font-medium">{s.name}</span>
                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                  {s.transport}
                </span>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
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
              {s.description && <p className="text-xs text-muted-foreground">{s.description}</p>}
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
      <section className="overflow-hidden rounded-xl border border-border">
        <h2 className="border-b border-border bg-card/40 px-4 py-3 text-sm font-semibold">
          {editing ? `Editar "${editing}"` : "Adicionar servidor"}
        </h2>
        <div className="flex flex-col gap-4 p-4">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <label className={labelCls}>
              Nome
              <input
                className={inputCls}
                value={form.name}
                placeholder="ex.: fetch"
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
            <label className={labelCls}>
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
            <label className={labelCls}>
              URL
              <input
                className={inputCls}
                value={form.url}
                placeholder="https://mcp.exemplo.com/sse"
                onChange={(e) => setForm({ ...form, url: e.target.value })}
              />
            </label>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <label className={labelCls}>
                Comando
                <input
                  className={inputCls}
                  value={form.command}
                  placeholder="npx"
                  onChange={(e) => setForm({ ...form, command: e.target.value })}
                />
              </label>
              <label className={labelCls}>
                <span>
                  Argumentos{" "}
                  <span className="font-normal text-muted-foreground">· separados por espaço</span>
                </span>
                <input
                  className={`${inputCls} font-mono text-xs`}
                  value={form.args}
                  placeholder="-y @modelcontextprotocol/server-fetch"
                  onChange={(e) => setForm({ ...form, args: e.target.value })}
                />
              </label>
            </div>
          )}
          <label className={labelCls}>
            <span>
              Descrição <span className="font-normal text-muted-foreground">· opcional</span>
            </span>
            <input
              className={inputCls}
              value={form.description}
              placeholder="Para que serve este servidor?"
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <div className="flex flex-wrap items-center gap-3 border-t border-border bg-card/40 px-4 py-3">
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
          <span className="text-[11px] text-muted-foreground">
            Mudanças valem para novas sessões do agente.
          </span>
        </div>
      </section>

      {/* Quick fill. Installing with a credential is the directory's job — it
          can collect the secret, and this form has nowhere to put one. */}
      {shelf.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Catálogo
            </h2>
            <span className="text-xs text-muted-foreground opacity-70">
              um clique preenche o formulário
            </span>
            <Link
              to={directoryPath}
              className="ml-auto text-xs text-brand-accent underline-offset-2 hover:underline"
              data-testid="mcp-directory-link"
            >
              Ver todos, inclusive os que pedem credencial →
            </Link>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {shelf.map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => prefill(c)}
                className="group flex items-start gap-3 rounded-xl border border-border bg-card/30 p-3 text-left transition-colors hover:border-foreground/20"
                data-testid={`mcp-shelf-${c.id}`}
              >
                <span
                  className="grid size-8 shrink-0 place-items-center rounded-lg text-[13px] font-bold text-black/85"
                  style={{ backgroundColor: avatarColor(c.id) }}
                  aria-hidden
                >
                  {avatarInitials(c.title, 2)}
                </span>
                <span className="flex min-w-0 flex-1 flex-col gap-1">
                  <span className="flex items-center gap-2">
                    <span className="truncate text-sm font-semibold">{c.title}</span>
                    <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                      {c.transport}
                    </span>
                  </span>
                  <span className="text-xs leading-snug text-muted-foreground">
                    {c.description}
                  </span>
                </span>
                <span
                  className="text-lg leading-none text-muted-foreground transition-colors group-hover:text-foreground"
                  aria-hidden
                >
                  +
                </span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
