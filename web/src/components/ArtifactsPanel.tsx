import { useMemo, useState } from "react";

import { XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";

interface Artifact {
  id: string;
  lang: string;
  code: string;
}

const FENCE = /```([\w.+-]*)[ \t]*\n([\s\S]*?)```/g;

/** Fenced code/doc blocks from the active conversation's finished assistant messages. */
function useArtifacts(): Artifact[] {
  const blocks = useChatStore((s) => s.blocks);
  return useMemo(() => {
    const out: Artifact[] = [];
    let n = 0;
    for (const b of blocks) {
      if (b.type !== "text_done" || !b.fullText) continue;
      FENCE.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = FENCE.exec(b.fullText)) !== null) {
        const code = m[2].replace(/\s+$/, "");
        if (code.trim()) out.push({ id: `${n++}`, lang: m[1] || "texto", code });
      }
    }
    return out;
  }, [blocks]);
}

function download(a: Artifact): void {
  const ext = a.lang && a.lang !== "texto" ? a.lang.split(/[.+]/)[0] : "txt";
  const blob = new Blob([a.code], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const el = document.createElement("a");
  el.href = url;
  el.download = `artifact-${a.id}.${ext}`;
  el.click();
  URL.revokeObjectURL(url);
}

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col overflow-hidden rounded-lg border border-border bg-card/40">
      <div className="flex items-center justify-between gap-2 border-border/60 border-b px-3 py-1.5">
        <span className="font-mono text-[11px] text-muted-foreground">{artifact.lang}</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => {
              void copyText(artifact.code);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="rounded px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            {copied ? "Copiado" : "Copiar"}
          </button>
          <button
            type="button"
            onClick={() => download(artifact)}
            className="rounded px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
          >
            Baixar
          </button>
        </div>
      </div>
      <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-xs leading-relaxed">
        <code>{artifact.code}</code>
      </pre>
    </div>
  );
}

/**
 * A self-contained right slide-over that collects the fenced code/doc blocks the
 * assistant produced in the active conversation. Reads the chat store directly,
 * so it needs no data props — only open/close.
 */
export function ArtifactsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const artifacts = useArtifacts();
  return (
    <div
      className={cn(
        "flex shrink-0 flex-col overflow-hidden border-border border-l bg-sidebar transition-[width] duration-200",
        open ? "w-[min(420px,80vw)]" : "w-0 border-l-0",
      )}
      aria-hidden={!open}
      inert={!open}
    >
      <div className="flex items-center justify-between px-4 pt-3 pb-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-sm">Artifacts</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground tabular-nums">
            {artifacts.length}
          </span>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label="Fechar Artifacts"
          onClick={onClose}
          className="rounded-full"
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto px-4 pb-4">
        {artifacts.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Os blocos de código e documentos que o assistente gerar nesta conversa aparecem aqui.
          </p>
        ) : (
          artifacts.map((a) => <ArtifactCard key={a.id} artifact={a} />)
        )}
      </div>
    </div>
  );
}
