import { useCallback, useEffect, useRef, useState } from "react";
import { FileTextIcon, TrashIcon, UploadIcon } from "lucide-react";

import { authenticatedFetch } from "@/lib/identity";
import { Link, useParams } from "@/lib/routing";
import { useProjectSessions } from "@/hooks/useConversations";

interface ProjectDocument {
  id: string;
  filename: string;
  bytes: number;
  content_type: string | null;
  text_chars: number;
  searchable: boolean;
  created_at: number;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * A project's own page: the documents its sessions can consult, plus the
 * sessions themselves.
 *
 * Projects were already a sidebar folder; what they lacked was a shelf. Upload
 * a contract or a spec once here and every session in the project reaches it
 * through the `project_knowledge` tool, instead of re-attaching the same file
 * to each conversation.
 */
export function ProjectPage() {
  const params = useParams();
  const project = decodeURIComponent(params.name ?? "");
  const [docs, setDocs] = useState<ProjectDocument[] | "loading">("loading");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const sessions = useProjectSessions(project, Boolean(project));

  const base = `/v1/projects/${encodeURIComponent(project)}`;

  const load = useCallback(async () => {
    if (!project) return;
    try {
      const res = await authenticatedFetch(`${base}/documents`);
      const data = res.ok ? ((await res.json()) as { data: ProjectDocument[] }).data : [];
      setDocs(data);
    } catch {
      setDocs([]);
    }
  }, [base, project]);

  useEffect(() => {
    void load();
  }, [load]);

  const upload = async (file: File) => {
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      // No explicit Content-Type: the browser sets the multipart boundary.
      const res = await authenticatedFetch(`${base}/documents`, { method: "POST", body: form });
      if (!res.ok) {
        const j = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
        setError(j?.error?.message ?? `Falha no envio (HTTP ${res.status}).`);
        return;
      }
      await load();
    } catch {
      setError("Falha de rede ao enviar.");
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const remove = async (doc: ProjectDocument) => {
    if (!window.confirm(`Remover "${doc.filename}" da base do projeto?`)) return;
    await authenticatedFetch(`${base}/documents/${encodeURIComponent(doc.id)}`, {
      method: "DELETE",
    });
    await load();
  };

  const sessionRows = (sessions.data?.pages ?? []).flatMap((p) => p.data ?? []);
  const searchable = docs === "loading" ? 0 : docs.filter((d) => d.searchable).length;

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">{project}</h1>
        <p className="text-sm opacity-60">
          Documentos aqui viram a base de conhecimento do projeto — qualquer sessão dele pode
          consultá-los pela ferramenta{" "}
          <code className="rounded bg-muted px-1">project_knowledge</code>.
        </p>
      </header>

      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide opacity-50">
            Base de conhecimento
            {docs !== "loading" && docs.length > 0 && (
              <span className="ml-2 font-normal normal-case opacity-70">
                {docs.length} documento(s), {searchable} pesquisável(is)
              </span>
            )}
          </h2>
          <label className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-brand-accent px-3 py-1.5 text-xs font-medium text-brand-accent-foreground transition hover:opacity-90">
            <UploadIcon className="size-3.5" />
            {busy ? "Enviando…" : "Adicionar documento"}
            <input
              ref={inputRef}
              type="file"
              className="hidden"
              disabled={busy}
              aria-label="Adicionar documento"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void upload(file);
              }}
            />
          </label>
        </div>

        {error && (
          <p className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">{error}</p>
        )}

        {docs === "loading" ? (
          <p className="text-sm opacity-50">Carregando…</p>
        ) : docs.length === 0 ? (
          <p className="text-sm opacity-40">
            Nenhum documento ainda. Envie um contrato, spec ou nota e as sessões deste projeto
            passam a poder consultá-lo.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {docs.map((d) => (
              <li
                key={d.id}
                className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card/40 p-3"
                data-testid={`doc-${d.id}`}
              >
                <FileTextIcon className="size-4 shrink-0 opacity-60" />
                <a
                  href={`${base}/documents/${encodeURIComponent(d.id)}/content`}
                  className="min-w-0 flex-1 truncate text-sm underline-offset-2 hover:underline"
                >
                  {d.filename}
                </a>
                <span className="text-[11px] opacity-50">{formatBytes(d.bytes)}</span>
                {d.searchable ? (
                  <span className="rounded bg-brand-accent/15 px-1.5 py-0.5 text-[11px] text-brand-accent">
                    pesquisável
                  </span>
                ) : (
                  <span
                    className="rounded bg-muted px-1.5 py-0.5 text-[11px] opacity-60"
                    title="Guardado e baixável, mas sem texto extraível para busca."
                  >
                    sem texto
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => void remove(d)}
                  aria-label={`Remover ${d.filename}`}
                  className="rounded-lg p-1 opacity-50 transition hover:bg-muted hover:opacity-100"
                >
                  <TrashIcon className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide opacity-50">
          Sessões do projeto
        </h2>
        {sessionRows.length === 0 ? (
          <p className="text-sm opacity-40">Nenhuma sessão neste projeto ainda.</p>
        ) : (
          <ul className="flex flex-col gap-1">
            {sessionRows.map((s: { id: string; title?: string | null }) => (
              <li key={s.id}>
                <Link
                  to={`/c/${encodeURIComponent(s.id)}`}
                  className="block truncate rounded-lg px-2 py-1.5 text-sm transition hover:bg-muted"
                >
                  {s.title || "Sem título"}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
