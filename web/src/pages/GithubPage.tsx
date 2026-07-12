import { useCallback, useEffect, useState } from "react";

import { setComposeSeed } from "@/lib/composeSeed";
import { authenticatedFetch } from "@/lib/identity";
import { useNavigate } from "@/lib/routing";

const LAST_REPO_KEY = "omnicraft.github.lastRepo";

interface GithubItem {
  number: number;
  title: string;
  url: string;
  state: string;
  author: string | null;
  comments: number;
  updated_at: string | null;
  is_pr: boolean;
  labels: string[];
}

interface GithubDetail extends GithubItem {
  body: string;
  comments_list?: { author: string | null; body: string }[];
}

type ItemKind = "issue" | "pr";

function relTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 60) return `${mins}min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** Build the starting prompt handed to the composer from an issue/PR. */
function buildSeed(repo: string, item: GithubDetail): string {
  const kind = item.is_pr ? "pull request" : "issue";
  const parts = [
    `Trabalhe nesta ${kind} do GitHub (${repo} #${item.number}):`,
    "",
    `# ${item.title}`,
    item.url,
  ];
  if (item.body.trim()) parts.push("", item.body.trim());
  const comments = item.comments_list ?? [];
  if (comments.length > 0) {
    parts.push("", "## Comentários");
    for (const c of comments) parts.push("", `**@${c.author ?? "?"}:** ${c.body}`);
  }
  return parts.join("\n").trim();
}

export function GithubPage() {
  const navigate = useNavigate();
  const [repoInput, setRepoInput] = useState(() => {
    try {
      return localStorage.getItem(LAST_REPO_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [repo, setRepo] = useState(repoInput);
  const [kind, setKind] = useState<ItemKind>("issue");
  const [state, setState] = useState<"open" | "closed" | "all">("open");
  const [status, setStatus] = useState<{ configured: boolean; login: string | null } | null>(null);
  const [items, setItems] = useState<GithubItem[] | "loading" | "error" | null>(null);
  const [detail, setDetail] = useState<GithubDetail | "loading" | "error" | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [listError, setListError] = useState<string | null>(null);

  useEffect(() => {
    void authenticatedFetch("/v1/integrations/github/status")
      .then((r) => (r.ok ? r.json() : { configured: false, login: null }))
      .then(setStatus)
      .catch(() => setStatus({ configured: false, login: null }));
  }, []);

  const loadItems = useCallback(async (r: string, k: ItemKind, s: "open" | "closed" | "all") => {
    if (!r.trim()) return;
    setItems("loading");
    setListError(null);
    setDetail(null);
    setSelected(null);
    try {
      const res = await authenticatedFetch(
        `/v1/integrations/github/items?repo=${encodeURIComponent(r.trim())}&type=${k}&state=${s}`,
      );
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: { message?: string } };
        setListError(j?.error?.message ?? `Erro ${res.status}`);
        setItems("error");
        return;
      }
      const j = (await res.json()) as { data: GithubItem[] };
      setItems(j.data);
    } catch {
      setListError("Falha de rede ao consultar o GitHub.");
      setItems("error");
    }
  }, []);

  const openRepo = () => {
    const r = repoInput.trim();
    if (!r) return;
    try {
      localStorage.setItem(LAST_REPO_KEY, r);
    } catch {
      /* ignore */
    }
    setRepo(r);
    void loadItems(r, kind, state);
  };

  const switchKind = (k: ItemKind) => {
    setKind(k);
    if (repo) void loadItems(repo, k, state);
  };

  const switchState = (s: "open" | "closed" | "all") => {
    setState(s);
    if (repo) void loadItems(repo, kind, s);
  };

  const openItem = async (item: GithubItem) => {
    setSelected(item.number);
    setDetail("loading");
    try {
      const res = await authenticatedFetch(
        `/v1/integrations/github/items/${item.number}?repo=${encodeURIComponent(repo)}`,
      );
      if (!res.ok) {
        setDetail("error");
        return;
      }
      const j = (await res.json()) as GithubDetail;
      setDetail({ ...j, comments: item.comments });
    } catch {
      setDetail("error");
    }
  };

  const startSession = (item: GithubDetail) => {
    setComposeSeed(buildSeed(repo, item));
    navigate("/");
  };

  const accent = { backgroundColor: "var(--brand-accent)" };

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-4 px-6 py-6">
      <header className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-xl font-semibold">Integração GitHub</h1>
          {status &&
            (status.configured ? (
              <span className="text-xs opacity-60">
                {status.login ? `Conectado como @${status.login}` : "Token configurado"}
              </span>
            ) : (
              <span className="text-xs text-amber-400">Nenhum token do GitHub</span>
            ))}
        </div>
        <p className="text-sm opacity-70">
          Navegue pelas issues e pull requests de um repositório e comece uma sessão já com o
          contexto do item — sem sair do OmniCraft.
        </p>
      </header>

      {status && !status.configured ? (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm">
          Nenhum token do GitHub disponível. Rode <code>gh auth login</code> na máquina do servidor
          ou defina <code>GITHUB_TOKEN</code> e reinicie o servidor.
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <input
          value={repoInput}
          onChange={(e) => setRepoInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && openRepo()}
          placeholder="owner/repositório  (ex.: cli/cli)"
          className="min-w-64 flex-1 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm outline-none focus:border-white/25"
        />
        <button
          type="button"
          onClick={openRepo}
          className="rounded-lg px-4 py-2 text-sm font-medium text-black"
          style={accent}
        >
          Carregar
        </button>
        <div className="ml-1 flex gap-1 rounded-lg border border-white/10 p-0.5">
          {(["issue", "pr"] as const).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => switchKind(k)}
              className={`rounded-md px-3 py-1.5 text-sm transition ${
                kind === k ? "bg-white/10 font-medium" : "opacity-60 hover:opacity-100"
              }`}
            >
              {k === "issue" ? "Issues" : "Pull Requests"}
            </button>
          ))}
        </div>
        {/* State filter — the backend already supports open/closed/all; a repo
            with nothing open otherwise looks broken. */}
        <div className="flex gap-1 rounded-lg border border-white/10 p-0.5">
          {(
            [
              ["open", "Abertas"],
              ["closed", "Fechadas"],
              ["all", "Todas"],
            ] as const
          ).map(([s, label]) => (
            <button
              key={s}
              type="button"
              onClick={() => switchState(s)}
              className={`rounded-md px-2.5 py-1.5 text-sm transition ${
                state === s ? "bg-white/10 font-medium" : "opacity-60 hover:opacity-100"
              }`}
              data-testid={`github-state-${s}`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        {/* List */}
        <div className="min-h-0 overflow-auto rounded-xl border border-white/10 bg-black/10">
          {items === null ? (
            <p className="p-6 text-sm opacity-50">Informe um repositório e carregue.</p>
          ) : items === "loading" ? (
            <p className="p-6 text-sm opacity-50">Carregando…</p>
          ) : items === "error" ? (
            <p className="p-6 text-sm text-red-400">{listError}</p>
          ) : items.length === 0 ? (
            <p className="p-6 text-sm opacity-50">
              {state === "open"
                ? `Nenhuma ${kind === "issue" ? "issue" : "PR"} aberta em ${repo} — a integração está funcionando; o repositório simplesmente não tem itens abertos. Experimente "Fechadas" ou a outra aba.`
                : "Nenhum item encontrado com este filtro."}
            </p>
          ) : (
            <ul className="divide-y divide-white/5">
              {items.map((it) => (
                <li key={it.number}>
                  <button
                    type="button"
                    onClick={() => void openItem(it)}
                    className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition hover:bg-white/5 ${
                      selected === it.number ? "bg-white/10" : ""
                    }`}
                  >
                    <span className="flex items-baseline gap-2">
                      <span className="shrink-0 text-xs opacity-40 tabular-nums">#{it.number}</span>
                      <span className="truncate text-sm font-medium">{it.title}</span>
                    </span>
                    <span className="flex items-center gap-2 text-xs opacity-50">
                      <span>@{it.author ?? "?"}</span>
                      <span>· {relTime(it.updated_at)}</span>
                      {it.comments > 0 && <span>· 💬 {it.comments}</span>}
                      {it.labels.slice(0, 2).map((l) => (
                        <span key={l} className="rounded bg-white/10 px-1.5 py-0.5">
                          {l}
                        </span>
                      ))}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Detail */}
        <div className="min-h-0 overflow-auto rounded-xl border border-white/10 bg-black/10">
          {detail === null ? (
            <p className="p-6 text-sm opacity-40">Selecione um item para ver os detalhes.</p>
          ) : detail === "loading" ? (
            <p className="p-6 text-sm opacity-40">Carregando…</p>
          ) : detail === "error" ? (
            <p className="p-6 text-sm text-red-400">Não foi possível carregar o item.</p>
          ) : (
            <div className="flex h-full flex-col">
              <div className="flex items-start justify-between gap-3 border-b border-white/10 px-5 py-4">
                <div className="min-w-0">
                  <h2 className="text-base font-semibold">
                    <span className="opacity-40">#{detail.number}</span> {detail.title}
                  </h2>
                  <p className="mt-0.5 text-xs opacity-50">
                    {detail.is_pr ? "Pull request" : "Issue"} · @{detail.author ?? "?"} ·{" "}
                    <a href={detail.url} target="_blank" rel="noreferrer" className="underline">
                      abrir no GitHub ↗
                    </a>
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() => startSession(detail)}
                  className="shrink-0 rounded-lg px-3 py-2 text-sm font-medium text-black"
                  style={accent}
                >
                  Iniciar sessão
                </button>
              </div>
              <div className="min-h-0 flex-1 overflow-auto px-5 py-4 text-sm leading-relaxed">
                {detail.body.trim() ? (
                  <p className="whitespace-pre-wrap opacity-90">{detail.body}</p>
                ) : (
                  <p className="opacity-40">(sem descrição)</p>
                )}
                {(detail.comments_list ?? []).length > 0 && (
                  <div className="mt-5 flex flex-col gap-3 border-t border-white/10 pt-4">
                    {(detail.comments_list ?? []).map((c, i) => (
                      <div key={i} className="rounded-lg bg-white/5 px-3 py-2">
                        <div className="mb-1 text-xs font-medium opacity-60">
                          @{c.author ?? "?"}
                        </div>
                        <p className="whitespace-pre-wrap text-sm opacity-90">{c.body}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
