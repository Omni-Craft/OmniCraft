import { HistoryIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { authenticatedFetch } from "@/lib/identity";

interface Snapshot {
  id: string;
  commit: string;
  label: string;
  created_at: number;
}

function relTime(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (mins < 1) return "agora";
  if (mins < 60) return `${mins}min`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/**
 * Header control for a session worktree's checkpoints (snapshot / restore).
 *
 * Self-contained and self-hiding: it probes the checkpoints endpoint on mount
 * and renders nothing for sessions without a git worktree (a 400), so chat-only
 * sessions stay uncluttered.
 */
export function CheckpointsButton({ sessionId }: { sessionId: string }) {
  const [hasWorktree, setHasWorktree] = useState<boolean | null>(null);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const base = `/v1/sessions/${encodeURIComponent(sessionId)}/checkpoints`;

  const refresh = useCallback(async (): Promise<boolean> => {
    try {
      const res = await authenticatedFetch(base);
      if (res.status === 400) {
        setHasWorktree(false);
        return false;
      }
      setHasWorktree(true);
      if (res.ok) {
        const j = (await res.json()) as { data: Snapshot[] };
        setSnapshots(Array.isArray(j.data) ? j.data : []);
      }
      return true;
    } catch {
      // Network hiccup — keep the button (a worktree session is still likely).
      setHasWorktree(true);
      return true;
    }
  }, [base]);

  useEffect(() => {
    setNote(null);
    setSnapshots([]);
    setHasWorktree(null);
    void refresh();
  }, [refresh]);

  const create = async () => {
    setBusy(true);
    setNote(null);
    try {
      const res = await authenticatedFetch(base, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() }),
      });
      if (!res.ok) {
        const j = (await res.json().catch(() => ({}))) as { error?: { message?: string } };
        setNote(j?.error?.message ?? "Falha ao criar o checkpoint.");
      } else {
        setLabel("");
        await refresh();
      }
    } catch {
      setNote("Falha de rede ao criar o checkpoint.");
    } finally {
      setBusy(false);
    }
  };

  const restore = async (snap: Snapshot) => {
    if (
      !window.confirm(
        `Restaurar o worktree para "${snap.label || "este checkpoint"}"?\n\nO estado atual é salvo automaticamente antes, então dá pra desfazer.`,
      )
    ) {
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const res = await authenticatedFetch(`${base}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ snapshot_id: snap.id }),
      });
      const j = (await res.json().catch(() => ({}))) as {
        backup_id?: string;
        error?: { message?: string };
      };
      if (!res.ok) {
        setNote(j?.error?.message ?? "Falha ao restaurar.");
      } else {
        setNote("Restaurado. O estado anterior virou um checkpoint (para desfazer).");
        await refresh();
      }
    } catch {
      setNote("Falha de rede ao restaurar.");
    } finally {
      setBusy(false);
    }
  };

  if (hasWorktree === false) return null;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="hidden h-8 gap-1.5 rounded-full px-3 text-13 font-normal md:inline-flex"
          aria-label="Checkpoints do worktree"
        >
          <HistoryIcon className="size-4" />
          Checkpoints
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="flex items-center gap-2 border-b border-border/60 p-3">
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !busy && void create()}
            placeholder="Rótulo (opcional)"
            className="min-w-0 flex-1 rounded-md border border-border/60 bg-transparent px-2 py-1.5 text-sm outline-none focus:border-border"
          />
          <Button
            type="button"
            size="sm"
            disabled={busy}
            onClick={() => void create()}
            className="h-8 shrink-0 rounded-md text-black"
            style={{ backgroundColor: "var(--brand-accent)" }}
          >
            Criar
          </Button>
        </div>

        {note && (
          <p className="border-b border-border/60 px-3 py-2 text-xs text-muted-foreground">{note}</p>
        )}

        <div className="max-h-72 overflow-auto">
          {hasWorktree === null ? (
            <p className="p-3 text-sm text-muted-foreground">Carregando…</p>
          ) : snapshots.length === 0 ? (
            <p className="p-3 text-sm text-muted-foreground">
              Nenhum checkpoint ainda. Crie um antes de uma mudança arriscada.
            </p>
          ) : (
            <ul className="divide-y divide-border/40">
              {snapshots.map((s) => (
                <li key={s.id} className="flex items-center justify-between gap-2 px-3 py-2">
                  <span className="min-w-0">
                    <span className="block truncate text-sm">{s.label || "sem rótulo"}</span>
                    <span className="text-xs text-muted-foreground">{relTime(s.created_at)}</span>
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={busy}
                    onClick={() => void restore(s)}
                    className="h-7 shrink-0 rounded-md text-xs"
                  >
                    Restaurar
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
