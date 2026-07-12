import { useMemo, useState } from "react";

import { XIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { setComposeSeed } from "@/lib/composeSeed";
import { useNavigate } from "@/lib/routing";
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

// Language → download-file extension. Fallback: a short alphanumeric lang is
// used as-is (already extension-like, e.g. "py"); anything else becomes "txt".
const LANG_EXTENSIONS: Record<string, string> = {
  python: "py",
  typescript: "ts",
  javascript: "js",
  tsx: "tsx",
  jsx: "jsx",
  "c++": "cpp",
  csharp: "cs",
  markdown: "md",
  text: "txt",
  texto: "txt",
  bash: "sh",
  shell: "sh",
  yaml: "yaml",
  json: "json",
  html: "html",
  css: "css",
  sql: "sql",
  go: "go",
  rust: "rs",
  ruby: "rb",
  java: "java",
  kotlin: "kt",
  swift: "swift",
};

function extensionForLang(lang: string): string {
  const key = lang.toLowerCase();
  if (LANG_EXTENSIONS[key]) return LANG_EXTENSIONS[key];
  return /^[a-z0-9]{1,4}$/i.test(lang) ? lang : "txt";
}

function download(a: Artifact): void {
  const ext = extensionForLang(a.lang);
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
  const navigate = useNavigate();
  // "Send to Code": seed the Code composer with this artifact as the task,
  // closing the plan-in-Chat → execute-in-Code loop (same seed mechanism the
  // GitHub page uses to start a session from an issue).
  const sendToCode = () => {
    setComposeSeed(
      `Implemente o seguinte (planejado no Chat):\n\n\`\`\`${artifact.lang}\n${artifact.code}\n\`\`\``,
    );
    navigate("/code");
  };
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
          <button
            type="button"
            onClick={sendToCode}
            title="Abrir o composer do Code com este artifact como tarefa"
            className="rounded bg-brand-accent/15 px-2 py-0.5 text-[11px] text-brand-accent transition-colors hover:bg-brand-accent/25"
            data-testid="artifact-send-to-code"
          >
            → Code
          </button>
        </div>
      </div>
      <pre className="max-h-72 overflow-auto px-3 py-2 font-mono text-xs leading-relaxed">
        <code>{artifact.code}</code>
      </pre>
    </div>
  );
}

/** Header + artifact list. Mounted only while the panel is open, so the
 * fence-parsing useArtifacts memo doesn't run on every streaming delta while
 * the panel is closed. */
function ArtifactsBody({ onClose }: { onClose: () => void }) {
  const artifacts = useArtifacts();
  return (
    <>
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
    </>
  );
}

/**
 * A self-contained right slide-over that collects the fenced code/doc blocks the
 * assistant produced in the active conversation. Reads the chat store directly,
 * so it needs no data props — only open/close. The outer div stays mounted for
 * the width transition; the content only mounts while open (no focusable
 * content when closed, so no React 18 `inert` workaround is needed).
 */
export function ArtifactsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <div
      className={cn(
        "flex shrink-0 flex-col overflow-hidden border-border border-l bg-sidebar transition-[width] duration-200",
        open ? "w-[min(420px,80vw)]" : "w-0 border-l-0",
      )}
      aria-hidden={!open}
    >
      {open && <ArtifactsBody onClose={onClose} />}
    </div>
  );
}
