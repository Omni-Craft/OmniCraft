import { useCallback, useEffect, useMemo, useState } from "react";

import { authenticatedFetch } from "@/lib/identity";

interface AgentRow {
  id: string;
  name: string;
  display_name?: string;
}

interface EnvVar {
  name: string;
  label: string;
  help?: string;
}

interface Connector {
  id: string;
  title: string;
  emoji: string;
  category: string;
  description: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  url?: string;
  env_required?: EnvVar[];
  setup_note?: string;
  docs_url?: string;
}

interface InstalledServer {
  name: string;
}

/**
 * The connector directory — a browsable catalog of MCP servers with one-click
 * install.
 *
 * Adding a server used to mean knowing its package name, transport and args.
 * Here a card carries all of that, so installing is: pick the agent, fill in
 * the credential the connector asks for (if any), click. The install POSTs to
 * the same endpoint the manual form uses and immediately tests the connection,
 * because "it saved" and "it works" are different claims.
 */
export function ConnectorsPage() {
  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [agentId, setAgentId] = useState("");
  const [catalog, setCatalog] = useState<Connector[] | "loading">("loading");
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [status, setStatus] = useState<Record<string, string>>({});

  useEffect(() => {
    void authenticatedFetch("/v1/agents")
      .then((r) => (r.ok ? r.json() : { data: [] }))
      .then((j: { data: AgentRow[] }) => {
        setAgents(j.data);
        const chat = j.data.find((a) => a.name === "chat");
        setAgentId((prev) => prev || chat?.id || j.data[0]?.id || "");
      })
      .catch(() => setAgents([]));
  }, []);

  useEffect(() => {
    void authenticatedFetch("/v1/mcp-catalog")
      .then((r) => (r.ok ? r.json() : { connectors: [] }))
      .then((j: { connectors: Connector[] }) => setCatalog(j.connectors))
      .catch(() => setCatalog([]));
  }, []);

  const loadInstalled = useCallback(async (id: string) => {
    if (!id) return;
    try {
      const res = await authenticatedFetch(`/v1/agents/${encodeURIComponent(id)}/mcp-servers`);
      const data = res.ok ? ((await res.json()) as { data: InstalledServer[] }).data : [];
      setInstalled(new Set(data.map((s) => s.name)));
    } catch {
      setInstalled(new Set());
    }
  }, []);

  useEffect(() => {
    void loadInstalled(agentId);
  }, [agentId, loadInstalled]);

  const shown = useMemo(() => {
    if (catalog === "loading") return [];
    const q = query.trim().toLowerCase();
    if (!q) return catalog;
    return catalog.filter((c) =>
      `${c.title} ${c.description} ${c.category}`.toLowerCase().includes(q),
    );
  }, [catalog, query]);

  const install = async (connector: Connector) => {
    setBusyId(connector.id);
    setStatus((s) => ({ ...s, [connector.id]: "Instalando…" }));
    try {
      const env: Record<string, string> = {};
      for (const v of connector.env_required ?? []) {
        const value = (secrets[`${connector.id}:${v.name}`] ?? "").trim();
        if (value) env[v.name] = value;
      }
      const payload: Record<string, unknown> = {
        name: connector.id,
        transport: connector.transport,
        description: connector.description.slice(0, 512),
      };
      if (connector.transport === "stdio") {
        payload.command = connector.command;
        payload.args = connector.args ?? [];
        if (Object.keys(env).length) payload.env = env;
      } else {
        payload.url = connector.url;
      }
      const res = await authenticatedFetch(
        `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!res.ok) {
        const j = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
        setStatus((s) => ({
          ...s,
          [connector.id]: `❌ ${j?.error?.message ?? `Falha (HTTP ${res.status}).`}`,
        }));
        return;
      }
      setOpenId(null);
      await loadInstalled(agentId);
      await testConnector(connector.id);
    } catch {
      setStatus((s) => ({ ...s, [connector.id]: "❌ Falha de rede." }));
    } finally {
      setBusyId(null);
    }
  };

  const testConnector = async (name: string) => {
    setStatus((s) => ({ ...s, [name]: "Testando conexão…" }));
    try {
      const res = await authenticatedFetch(
        `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers/${encodeURIComponent(name)}/test`,
        { method: "POST" },
      );
      const j = (await res.json()) as { ok: boolean; tool_count?: number; error?: string };
      setStatus((s) => ({
        ...s,
        [name]: j.ok ? `✅ conectado — ${j.tool_count} tools` : `❌ ${j.error}`,
      }));
    } catch {
      setStatus((s) => ({ ...s, [name]: "❌ Falha ao testar." }));
    }
  };

  const uninstall = async (name: string) => {
    if (!window.confirm(`Remover o conector "${name}" deste agente?`)) return;
    await authenticatedFetch(
      `/v1/agents/${encodeURIComponent(agentId)}/mcp-servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
    setStatus((s) => ({ ...s, [name]: "" }));
    await loadInstalled(agentId);
  };

  const inputCls =
    "w-full rounded-lg border border-border bg-card/40 px-3 py-2 text-sm outline-none focus:border-ring";

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Diretório de conectores</h1>
        <p className="text-sm opacity-60">
          Conectores MCP prontos para instalar num agente. As credenciais que você digitar vão para
          o bundle do agente e não voltam por nenhuma tela — prefira uma referência{" "}
          <code className="rounded bg-muted px-1">{"${VARIAVEL}"}</code> quando puder.
        </p>
      </header>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-xs opacity-70">
          Agente
          <select className={inputCls} value={agentId} onChange={(e) => setAgentId(e.target.value)}>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.display_name ?? a.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-[12rem] flex-1 flex-col gap-1 text-xs opacity-70">
          Buscar
          <input
            className={inputCls}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="github, banco de dados, navegador…"
            aria-label="Buscar conectores"
          />
        </label>
      </div>

      {catalog === "loading" ? (
        <p className="text-sm opacity-50">Carregando catálogo…</p>
      ) : shown.length === 0 ? (
        <p className="text-sm opacity-40">Nenhum conector encontrado para essa busca.</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {shown.map((c) => {
            const isInstalled = installed.has(c.id);
            const isOpen = openId === c.id;
            return (
              <div
                key={c.id}
                className="flex flex-col gap-2 rounded-xl border border-border bg-card/40 p-3"
                data-testid={`connector-${c.id}`}
              >
                <div className="flex items-start gap-2">
                  <span className="text-xl leading-none">{c.emoji}</span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm font-medium">{c.title}</span>
                      {isInstalled && (
                        <span className="rounded bg-brand-accent/15 px-1.5 py-0.5 text-[11px] text-brand-accent">
                          instalado
                        </span>
                      )}
                    </div>
                    <p className="text-xs opacity-60">{c.description}</p>
                  </div>
                </div>

                {c.setup_note && <p className="text-[11px] opacity-50">⚠️ {c.setup_note}</p>}

                {isOpen && (c.env_required?.length ?? 0) > 0 && (
                  <div className="flex flex-col gap-2 rounded-lg bg-muted/40 p-2">
                    {c.env_required?.map((v) => (
                      <label key={v.name} className="flex flex-col gap-1 text-[11px] opacity-70">
                        {v.label}
                        <input
                          className={inputCls}
                          type="password"
                          autoComplete="off"
                          value={secrets[`${c.id}:${v.name}`] ?? ""}
                          onChange={(e) =>
                            setSecrets((s) => ({ ...s, [`${c.id}:${v.name}`]: e.target.value }))
                          }
                          placeholder={`\${${v.name}}`}
                          aria-label={v.label}
                        />
                        {v.help && <span className="opacity-60">{v.help}</span>}
                      </label>
                    ))}
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-2">
                  {isInstalled ? (
                    <>
                      <button
                        type="button"
                        onClick={() => void testConnector(c.id)}
                        className="rounded-lg bg-brand-accent/15 px-2.5 py-1 text-xs text-brand-accent transition hover:bg-brand-accent/25"
                      >
                        Testar
                      </button>
                      <button
                        type="button"
                        onClick={() => void uninstall(c.id)}
                        className="rounded-lg px-2.5 py-1 text-xs opacity-60 transition hover:opacity-100"
                      >
                        Remover
                      </button>
                    </>
                  ) : (
                    <button
                      type="button"
                      disabled={busyId === c.id || !agentId}
                      onClick={() => {
                        if ((c.env_required?.length ?? 0) > 0 && !isOpen) {
                          setOpenId(c.id);
                          return;
                        }
                        void install(c);
                      }}
                      className="rounded-lg bg-brand-accent px-2.5 py-1 text-xs font-medium text-brand-accent-foreground transition hover:opacity-90 disabled:opacity-40"
                      data-testid={`install-${c.id}`}
                    >
                      {busyId === c.id ? "Instalando…" : isOpen ? "Confirmar" : "Instalar"}
                    </button>
                  )}
                  {c.docs_url && (
                    <a
                      href={c.docs_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs opacity-50 underline-offset-2 transition hover:underline hover:opacity-80"
                    >
                      docs
                    </a>
                  )}
                  {status[c.id] && (
                    <span className="min-w-0 flex-1 truncate text-[11px] opacity-70">
                      {status[c.id]}
                    </span>
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
