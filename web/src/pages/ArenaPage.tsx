import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAvailableAgents, type AvailableAgent } from "@/hooks/useAvailableAgents";
import { useHosts, type Host } from "@/hooks/useHosts";
import { authenticatedFetch } from "@/lib/identity";
import { fetchLastAssistantText } from "@/lib/lastAssistantText";
import {
  isNativeCodingAgent,
  nativeDisplayNameForAgent,
  nativeWrapperLabelsForAgent,
} from "@/lib/nativeCodingAgents";
import { useNavigate, useParams } from "@/lib/routing";

// Mirror of omnicraft.stores.conversation_store.ARENA_GROUP_LABEL_KEY and the
// sibling arena.* keys. Every racer in one arena carries these so the
// comparison view can list them (?arena_group=<id>) and order/label the cards.
const ARENA_GROUP_LABEL_KEY = "omnicraft.arena.group";
const ARENA_INDEX_LABEL_KEY = "omnicraft.arena.index";
const ARENA_PROMPT_LABEL_KEY = "omnicraft.arena.prompt";

const REFRESH_MS = 3000;

/** A racer session as returned by GET /v1/sessions (the fields we render). */
interface RacerSession {
  id: string;
  agent_name?: string | null;
  status?: string | null;
  labels?: Record<string, string> | null;
  workspace?: string | null;
}

/** Short random suffix for arena ids and worktree branches. */
function randomSuffix(): string {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
}

function branchSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24);
}

function statusTone(status: string | null | undefined): { dot: string; label: string } {
  switch (status) {
    case "in_progress":
    case "working":
    case "running":
      return { dot: "#e3a008", label: "trabalhando" };
    case "error":
    case "failed":
      return { dot: "#e5484d", label: "erro" };
    case "idle":
    case "completed":
    case "done":
      return { dot: "#30a46c", label: "pronto" };
    default:
      return { dot: "#8b949e", label: status ?? "—" };
  }
}

// ── Setup: choose a prompt + harnesses, then spawn the racers ──────────────

function ArenaSetup() {
  const navigate = useNavigate();
  const { data: agents } = useAvailableAgents();
  const { data: hosts } = useHosts();

  const [prompt, setPrompt] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [baseBranch, setBaseBranch] = useState("");
  const [hostId, setHostId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const harnesses = useMemo(
    () => (agents ?? []).filter((a) => isNativeCodingAgent(a)),
    [agents],
  );
  const onlineHosts = useMemo(
    () => (hosts ?? []).filter((h: Host) => h.status === "online"),
    [hosts],
  );

  // Preselect the single online host so a common case needs no click.
  useEffect(() => {
    if (hostId === null && onlineHosts.length === 1) setHostId(onlineHosts[0].host_id);
  }, [hostId, onlineHosts]);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const workspaceValid = workspace.trim().startsWith("/");
  const canSubmit =
    prompt.trim().length > 0 &&
    workspaceValid &&
    hostId !== null &&
    selected.size >= 2 &&
    !submitting;

  const start = async () => {
    if (!canSubmit || hostId === null) return;
    setSubmitting(true);
    setError(null);
    const arenaId = `arena_${randomSuffix()}`;
    const shortId = arenaId.slice(-6);
    const ws = workspace.trim();
    const base = baseBranch.trim() || undefined;
    const racers = harnesses.filter((a) => selected.has(a.id));

    const results = await Promise.allSettled(
      racers.map((agent, index) => {
        const labels: Record<string, string> = {
          ...(nativeWrapperLabelsForAgent(agent) ?? {}),
          [ARENA_GROUP_LABEL_KEY]: arenaId,
          [ARENA_INDEX_LABEL_KEY]: String(index),
          [ARENA_PROMPT_LABEL_KEY]: prompt.trim().slice(0, 500),
        };
        return authenticatedFetch("/v1/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            agent_id: agent.id,
            host_id: hostId,
            workspace: ws,
            git: { branch_name: `arena-${shortId}-${branchSlug(agent.name)}`, base_branch: base },
            labels,
            initial_items: [
              {
                type: "message",
                data: { role: "user", content: [{ type: "input_text", text: prompt.trim() }] },
              },
            ],
          }),
        }).then(async (res) => {
          if (!res.ok) throw new Error(`${nativeDisplayNameForAgent(agent)}: ${res.status}`);
        });
      }),
    );

    const failures = results.filter((r) => r.status === "rejected");
    if (failures.length === racers.length) {
      setSubmitting(false);
      setError(
        `Nenhum racer pôde ser criado. ${(failures[0] as PromiseRejectedResult).reason}`,
      );
      return;
    }
    if (failures.length > 0) {
      setError(
        `${failures.length} de ${racers.length} racers falharam; abrindo os que subiram.`,
      );
    }
    navigate(`/arena/${arenaId}`);
  };

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold">Arena de agentes</h1>
        <p className="text-sm opacity-70">
          Mande o <b>mesmo prompt</b> para vários harnesses ao mesmo tempo, cada um no seu
          worktree isolado, e compare os resultados lado a lado.
        </p>
      </header>

      <label className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">Prompt</span>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={4}
          placeholder="Descreva a tarefa que todos os agentes vão resolver…"
          className="resize-y rounded-lg border border-white/10 bg-black/20 p-3 text-sm outline-none focus:border-white/25"
        />
      </label>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">Diretório de trabalho</span>
          <input
            value={workspace}
            onChange={(e) => setWorkspace(e.target.value)}
            placeholder="/caminho/para/o/repo"
            className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/25"
          />
          {workspace.length > 0 && !workspaceValid && (
            <span className="text-xs text-red-400">Use um caminho absoluto (começa com /).</span>
          )}
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-sm font-medium">
            Branch base <span className="opacity-50">(opcional)</span>
          </span>
          <input
            value={baseBranch}
            onChange={(e) => setBaseBranch(e.target.value)}
            placeholder="main"
            className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/25"
          />
        </label>
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">Máquina</span>
        {onlineHosts.length === 0 ? (
          <p className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm opacity-70">
            Nenhuma máquina online. Registre um host (<code>omnicraft host</code>) para rodar a
            arena.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {onlineHosts.map((h: Host) => (
              <button
                key={h.host_id}
                type="button"
                onClick={() => setHostId(h.host_id)}
                className={`rounded-full border px-3 py-1.5 text-sm transition ${
                  hostId === h.host_id
                    ? "border-transparent text-black"
                    : "border-white/15 hover:border-white/30"
                }`}
                style={hostId === h.host_id ? { backgroundColor: "var(--brand-accent)" } : undefined}
              >
                {h.name}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium">
          Competidores <span className="opacity-50">(escolha 2 ou mais)</span>
        </span>
        <div className="flex flex-wrap gap-2">
          {harnesses.map((agent: AvailableAgent) => {
            const on = selected.has(agent.id);
            return (
              <button
                key={agent.id}
                type="button"
                onClick={() => toggle(agent.id)}
                className={`rounded-full border px-3 py-1.5 text-sm transition ${
                  on ? "border-transparent text-black" : "border-white/15 hover:border-white/30"
                }`}
                style={on ? { backgroundColor: "var(--brand-accent)" } : undefined}
              >
                {nativeDisplayNameForAgent(agent)}
              </button>
            );
          })}
        </div>
      </div>

      {error && <p className="text-sm text-red-400">{error}</p>}

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={!canSubmit}
          onClick={() => void start()}
          className="rounded-lg px-4 py-2 text-sm font-medium text-black transition disabled:cursor-not-allowed disabled:opacity-40"
          style={{ backgroundColor: "var(--brand-accent)" }}
        >
          {submitting ? "Criando racers…" : `Iniciar arena (${selected.size})`}
        </button>
        <span className="text-xs opacity-60">
          Cada competidor roda num worktree git próprio.
        </span>
      </div>
    </div>
  );
}

// ── Comparison: watch the racers side by side ─────────────────────────────

function RacerCard({ racer, onOpen }: { racer: RacerSession; onOpen: (id: string) => void }) {
  const [text, setText] = useState<string | undefined>(undefined);
  const tone = statusTone(racer.status);

  useEffect(() => {
    let alive = true;
    const load = () =>
      void fetchLastAssistantText(racer.id, 400).then((t) => {
        if (alive) setText(t);
      });
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [racer.id]);

  const branch = racer.labels?.["omnicraft.arena.index"];
  return (
    <div className="flex min-h-[220px] flex-col rounded-xl border border-white/10 bg-black/20">
      <div className="flex items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
        <span className="truncate text-sm font-semibold">
          {racer.agent_name ?? "agente"}
          {branch !== undefined && <span className="ml-1.5 opacity-40">#{Number(branch) + 1}</span>}
        </span>
        <span className="flex shrink-0 items-center gap-1.5 text-xs opacity-80">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ backgroundColor: tone.dot }}
          />
          {tone.label}
        </span>
      </div>
      <div className="flex-1 overflow-auto px-4 py-3 text-sm leading-relaxed opacity-90">
        {text ? (
          <p className="whitespace-pre-wrap">{text}</p>
        ) : (
          <p className="opacity-40">Aguardando a primeira resposta…</p>
        )}
      </div>
      <div className="flex items-center justify-between border-t border-white/10 px-4 py-2.5">
        <span className="truncate text-xs opacity-40">{racer.workspace}</span>
        <button
          type="button"
          onClick={() => onOpen(racer.id)}
          className="shrink-0 rounded-md border border-white/15 px-2.5 py-1 text-xs transition hover:border-white/30"
        >
          Abrir sessão →
        </button>
      </div>
    </div>
  );
}

function ArenaCompare({ arenaId }: { arenaId: string }) {
  const navigate = useNavigate();
  const [racers, setRacers] = useState<RacerSession[] | null>(null);
  const seenRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await authenticatedFetch(
        `/v1/sessions?arena_group=${encodeURIComponent(arenaId)}&limit=50`,
      );
      if (!res.ok) return;
      const json = (await res.json()) as { data?: RacerSession[] };
      const list = Array.isArray(json.data) ? json.data : [];
      list.sort(
        (a, b) =>
          Number(a.labels?.[ARENA_INDEX_LABEL_KEY] ?? 0) -
          Number(b.labels?.[ARENA_INDEX_LABEL_KEY] ?? 0),
      );
      setRacers(list);
      seenRef.current = true;
    } catch {
      /* transient network error — keep the last snapshot */
    }
  }, [arenaId]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const arenaPrompt = useMemo(
    () => racers?.find((r) => r.labels?.[ARENA_PROMPT_LABEL_KEY])?.labels?.[ARENA_PROMPT_LABEL_KEY],
    [racers],
  );

  if (racers === null) {
    return <div className="px-6 py-10 text-sm opacity-60">Carregando arena…</div>;
  }
  if (racers.length === 0) {
    return (
      <div className="mx-auto flex max-w-md flex-col items-center gap-4 px-6 py-16 text-center">
        <p className="text-sm opacity-70">
          Nenhum competidor encontrado para esta arena.
        </p>
        <button
          type="button"
          onClick={() => navigate("/arena")}
          className="rounded-lg px-4 py-2 text-sm font-medium text-black"
          style={{ backgroundColor: "var(--brand-accent)" }}
        >
          Nova arena
        </button>
      </div>
    );
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-5 px-6 py-8">
      <header className="flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-xl font-semibold">
            Arena · {racers.length} competidores
          </h1>
          <button
            type="button"
            onClick={() => navigate("/arena")}
            className="rounded-md border border-white/15 px-3 py-1.5 text-sm transition hover:border-white/30"
          >
            Nova arena
          </button>
        </div>
        {arenaPrompt && (
          <p className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm opacity-80">
            {arenaPrompt}
          </p>
        )}
      </header>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {racers.map((r) => (
          <RacerCard key={r.id} racer={r} onOpen={(id) => navigate(`/c/${id}`)} />
        ))}
      </div>
    </div>
  );
}

export function ArenaPage() {
  const { arenaId } = useParams<"arenaId">();
  return arenaId ? <ArenaCompare arenaId={arenaId} /> : <ArenaSetup />;
}
