/**
 * The floating HUD's content — a glanceable answer to "what is running, and
 * what needs me", rendered at `/hud` outside the app shell.
 *
 * Collapsed it is a single pill (`N ativas · M aguardando`). Expanded it lists
 * the sessions and, when one is blocked on a human, lets the verdict be given
 * right here: the same `ApprovalCard` the chat and the inbox render, with the
 * POST routed to `pending_elicitation.session_id` — which can be a sub-agent
 * CHILD of the row it appears on, and is the session that owns the parked
 * prompt.
 *
 * Nothing the feed leaves unresolved is rounded off. An unreadable feed says
 * so instead of showing an all-clear; a truncated one says the list is
 * partial; `null` liveness and `null` cost read "desconhecido" / "—", never
 * "offline" / "US$ 0.00"; and a status the server invented renders as unknown
 * rather than breaking the badge.
 *
 * Expanding also asks the Electron shell to resize its window (the shell picks
 * the bounds — see `lib/hudBridge`). Outside the shell that call is a no-op, so
 * the same route works in a plain browser tab.
 */

import { useState } from "react";
import { AlertTriangleIcon, ChevronDownIcon, Loader2Icon } from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { getSessionState } from "@/hooks/useSessionState";
import { useMonitorFeed, type MonitorSession, type MonitorStatus } from "@/hooks/useMonitorFeed";
import { setHudExpanded } from "@/lib/hudBridge";
import { approve } from "@/lib/sessionsApi";
import { cn } from "@/lib/utils";

const STATUS_LABELS: Record<MonitorStatus, string> = {
  idle: "ocioso",
  launching: "iniciando",
  running: "em execução",
  waiting: "aguardando você",
  failed: "falhou",
  unknown: "estado desconhecido",
};

/** The row's headline: the project it's filed under, else its own identity. */
function sessionLabel(session: MonitorSession): string {
  return session.project ?? session.title ?? session.agentName ?? session.sessionId;
}

/**
 * Liveness copy. `null` is UNKNOWN and must read neutral — reporting it as
 * offline would invent a fact the server explicitly declined to assert.
 */
function livenessText(online: boolean | null): { text: string; tone: "unknown" | "up" | "down" } {
  if (online === null) return { text: "desconhecido", tone: "unknown" };
  return online ? { text: "online", tone: "up" } : { text: "offline", tone: "down" };
}

export interface HudPanelProps {
  /** Filter the feed to one host. Unset watches every visible session. */
  hostId?: string | null;
  /** Held false until the viewer identity resolves; see `HudPage`. */
  enabled?: boolean;
  /**
   * Notified on every expand/collapse so the shell can resize its window.
   * Injectable for tests; defaults to the real bridge.
   */
  onExpandedChange?: (expanded: boolean) => void;
}

export function HudPanel({
  hostId = null,
  enabled = true,
  onExpandedChange = setHudExpanded,
}: HudPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [responded, setResponded] = useState<Record<string, "accept" | "decline">>({});
  const query = useMonitorFeed({ hostId, enabled });
  const feed = query.data ?? null;

  // Two different failures, one meaning: we do not know what is running.
  // A fetch that never landed and a feed that reports itself unbuildable are
  // equally not "nothing is running".
  const unreadable = query.isError || feed?.unreadable === true;
  // No snapshot yet — booting, disabled while identity resolves, or a first
  // read still in flight. Falling through to the counts here would paint
  // "0 ativas · 0 aguardando", which is an all-clear we have not earned.
  const loading = !feed && !unreadable;

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    onExpandedChange(next);
  };

  const makeSubmit = (resolveSessionId: string): SubmitApprovalFn => {
    return (elicitationId, action, content) => {
      setResponded((prev) => ({ ...prev, [elicitationId]: action }));
      void approve(
        resolveSessionId,
        elicitationId,
        content === undefined ? { action } : { action, content },
      ).then(
        () => query.refetch(),
        () => {
          // Roll back so the buttons come back and the user can retry —
          // same recovery the chat store and the inbox use.
          setResponded((prev) => {
            const next = { ...prev };
            delete next[elicitationId];
            return next;
          });
        },
      );
    };
  };

  let pill: string;
  if (unreadable) pill = "Feed indisponível";
  else if (loading || !feed) pill = "Carregando…";
  else
    pill = `${feed.counts.active} ativas · ${feed.counts.awaiting} aguardando${
      feed.truncated ? " · parcial" : ""
    }`;

  return (
    <div
      data-testid="hud-panel"
      data-expanded={expanded}
      className="flex max-h-screen flex-col overflow-hidden rounded-xl border border-border bg-card/95 shadow-lg backdrop-blur"
    >
      <button
        type="button"
        data-testid="hud-pill"
        aria-expanded={expanded}
        onClick={toggle}
        className="flex h-11 shrink-0 items-center gap-2 px-3 text-left text-sm"
      >
        {unreadable ? (
          <AlertTriangleIcon className="size-4 shrink-0 text-warning" aria-hidden />
        ) : loading ? (
          <Loader2Icon className="size-4 shrink-0 animate-spin text-muted-foreground" aria-hidden />
        ) : (
          <ChevronDownIcon
            className={cn(
              "size-4 shrink-0 text-muted-foreground transition-transform",
              !expanded && "-rotate-90",
            )}
            aria-hidden
          />
        )}
        <span className="min-w-0 flex-1 truncate font-medium">{pill}</span>
      </button>

      {expanded && (
        <div data-testid="hud-body" className="flex flex-col gap-2 overflow-y-auto px-3 pb-3">
          {unreadable && (
            <p data-testid="hud-unreadable" className="text-xs text-warning">
              Não foi possível ler o feed de sessões. O que está rodando é desconhecido — isto não
              quer dizer que nada está rodando.
            </p>
          )}
          {feed?.truncated && (
            <p data-testid="hud-truncated" className="text-xs text-muted-foreground">
              Lista parcial: há mais sessões do que cabe no feed.
            </p>
          )}
          {/* "Nothing running" is a claim about a feed we actually READ —
              never a stand-in for one we haven't got yet. */}
          {feed && !feed.unreadable && feed.sessions.length === 0 && !feed.truncated && (
            <p data-testid="hud-empty" className="text-xs text-muted-foreground">
              Nada em execução.
            </p>
          )}
          {loading && (
            <p data-testid="hud-loading" className="text-xs text-muted-foreground">
              Lendo o feed de sessões…
            </p>
          )}
          <ul className="flex flex-col gap-2">
            {(feed?.sessions ?? []).map((session) => (
              <HudSessionRow
                key={session.sessionId}
                session={session}
                responded={responded}
                makeSubmit={makeSubmit}
              />
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

interface HudSessionRowProps {
  session: MonitorSession;
  responded: Record<string, "accept" | "decline">;
  makeSubmit: (resolveSessionId: string) => SubmitApprovalFn;
}

function HudSessionRow({ session, responded, makeSubmit }: HudSessionRowProps) {
  const prompt = session.pendingElicitation;
  // The sidebar's own derivation, fed the monitor row: a pending prompt wins
  // over "running", which is exactly the priority a monitor wants.
  const badgeState = getSessionState({
    status: session.status === "running" ? "running" : undefined,
    pending_elicitations_count: session.pendingElicitationsCount,
  });
  const runner = livenessText(session.runnerOnline);
  const host = livenessText(session.hostOnline);
  const waiting = session.status === "waiting" || session.pendingElicitationsCount > 0;
  const verdict = prompt ? responded[prompt.id] : undefined;

  return (
    <li
      data-testid="hud-session"
      data-session-id={session.sessionId}
      data-status={session.status}
      data-waiting={waiting}
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-2",
        waiting ? "border-warning/40 bg-warning/5" : "border-border",
      )}
    >
      <div className="flex items-center gap-2">
        {badgeState && <SessionStateBadge state={badgeState} />}
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{sessionLabel(session)}</span>
        <span data-testid="hud-session-status" className="shrink-0 text-xs text-muted-foreground">
          {STATUS_LABELS[session.status]}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
        <span data-testid="hud-runner" data-tone={runner.tone}>
          runner: {runner.text}
        </span>
        <span data-testid="hud-host" data-tone={host.tone}>
          host: {host.text}
        </span>
        <span
          data-testid="hud-cost"
          data-tone={session.costUsd === null ? "unknown" : "known"}
          // An unrecorded cost is unknown, not zero — a dash, never "US$ 0,00".
        >
          custo: {session.costUsd === null ? "—" : `US$ ${session.costUsd.toFixed(2)}`}
        </span>
        {session.degraded.length > 0 && (
          <span data-testid="hud-session-degraded" className="text-warning">
            parcial: {session.degraded.join(", ")}
          </span>
        )}
      </div>
      {prompt && (
        <ApprovalCard
          elicitationId={prompt.id}
          message={prompt.summary ?? "Aprovação pendente"}
          phase=""
          policyName=""
          contentPreview=""
          requestedSchema={{}}
          status={verdict ? "responded" : "pending"}
          response={verdict ? { action: verdict } : null}
          // The prompt is parked on the session named by the feed, which may
          // be a sub-agent child of this row — post the verdict THERE.
          onSubmit={makeSubmit(prompt.sessionId)}
        />
      )}
      {!prompt && session.pendingElicitationsCount > 0 && (
        <p data-testid="hud-prompt-unreadable" className="text-xs text-warning">
          {session.pendingElicitationsCount} aprovação(ões) pendente(s), mas o conteúdo não pôde ser
          lido. Abra a sessão para responder.
        </p>
      )}
    </li>
  );
}
