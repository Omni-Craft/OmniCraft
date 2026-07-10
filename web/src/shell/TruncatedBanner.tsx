import { AlertTriangleIcon } from "lucide-react";

/**
 * Warning shown when the server returned only a prefix of a large file
 * (``truncated: true``). Shared by every file surface — the Monaco editor, the
 * TipTap markdown editor, and the read-only Shiki source view — so the message
 * and styling stay consistent and editing stays disabled to prevent data loss.
 *
 * @returns The truncation warning banner.
 */
export function TruncatedBanner() {
  return (
    <div className="flex items-center gap-2 border-b border-border bg-warning/10 px-4 py-1.5 text-xs text-foreground shrink-0">
      <AlertTriangleIcon className="size-3.5 shrink-0 text-warning" />
      <span>
        Este arquivo é grande demais para carregar por completo — exibindo uma prévia truncada. A
        edição está desativada para evitar sobrescrever o restante do arquivo; baixe-o para
        visualizar ou editar o conteúdo completo.
      </span>
    </div>
  );
}
