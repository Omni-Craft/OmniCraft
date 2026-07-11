// Sidebar status indicator. Every state stays a compact marker that fits the
// row's trailing slot without crowding the title: approval is a distinct amber
// bell, running is a grey spinner, unseen is a brand-pink dot. The full copy
// (incl. the approval count) lives in the tooltip + accessible label.

import { RunningDot } from "@/components/RunningDot";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { SessionState } from "@/hooks/useSessionState";
import { cn } from "@/lib/utils";
import { BellIcon } from "lucide-react";
import type { ReactElement } from "react";

export interface SessionStateBadgeProps {
  state: SessionState;
}

interface Visual {
  kind: SessionState["kind"];
  ariaLabel: string;
  tooltip: string;
  render: () => ReactElement;
}

function describe(state: SessionState): Visual {
  switch (state.kind) {
    case "awaiting": {
      const tooltip =
        state.count === 1
          ? "1 prompt de aprovação aguardando"
          : `${state.count} prompts de aprovação aguardando`;
      return {
        kind: state.kind,
        ariaLabel: tooltip,
        tooltip,
        render: () => (
          <span className="inline-flex size-5 items-center justify-center rounded-full bg-warning/20 text-warning ring-1 ring-warning/30">
            <BellIcon className="size-3" aria-hidden />
            <span className="sr-only">Aguardando resposta</span>
          </span>
        ),
      };
    }
    case "running":
      return {
        kind: state.kind,
        ariaLabel: "Sessão em execução",
        tooltip: "Sessão em execução",
        render: () => <RunningDot />,
      };
    case "unseen":
      // Solid brand-pink dot — distinguished from the running indicator,
      // which is a grey spinner.
      return {
        kind: state.kind,
        ariaLabel: "Novas mensagens",
        tooltip: "Novas mensagens",
        render: () => <Dot tone="bg-brand-accent" />,
      };
  }
}

function Dot({ tone }: { tone: string }) {
  return <span aria-hidden className={cn("size-2 shrink-0 rounded-full", tone)} />;
}

export function SessionStateBadge({ state }: SessionStateBadgeProps) {
  const visual = describe(state);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="session-state-badge"
          data-state={visual.kind}
          role="img"
          aria-label={visual.ariaLabel}
          className="inline-flex h-5 shrink-0 items-center justify-center"
        >
          {visual.render()}
        </span>
      </TooltipTrigger>
      {/* Opens left: the badge sits at the right edge of the narrow
          sidebar, so a right-opening tooltip would overflow the panel. */}
      <TooltipContent side="left">{visual.tooltip}</TooltipContent>
    </Tooltip>
  );
}
