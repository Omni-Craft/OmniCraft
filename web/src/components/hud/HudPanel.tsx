/**
 * The floating HUD's content — a glanceable answer to "what is running, and
 * what needs me", rendered at `/hud` outside the app shell.
 *
 * Collapsed it is a single pill; expanded it lists the sessions and, when one
 * is blocked on a human, lets the verdict be given right here.
 *
 * Everything here follows one rule: **the HUD may only assert what it knows.**
 * A monitor that answers "nothing needs you" when it has no idea is worse than
 * no monitor, so each way of not-knowing gets its own visible state:
 *
 *   - counts the payload didn't carry → "contagens ilegíveis", not zeros
 *   - a feed that failed to build, or a read that failed → the reason, with
 *     the note that this does NOT mean nothing is running
 *   - a snapshot that stopped refreshing → marked desatualizado, with its age,
 *     so old numbers are never mistaken for current ones
 *   - any degraded slug the server reports, including ones this build has
 *     never seen → shown, never ignored
 *   - `null` liveness/cost → "desconhecido" / "—", never "offline" / US$ 0,00
 *
 * Rows render in the SERVER's order, which is already ranked by how much each
 * session needs a human (blocked → failed → active → unresolved → idle). This
 * component must never re-sort them.
 *
 * Expanding also asks the Electron shell to resize its window (the shell picks
 * the bounds — see `lib/hudBridge`). Outside the shell that call is a no-op, so
 * the same route works in a plain browser tab.
 */

import { useEffect, useState } from "react";
import { AlertTriangleIcon, ChevronDownIcon, ClockAlertIcon, Loader2Icon } from "lucide-react";
import { ApprovalCard, type SubmitApprovalFn } from "@/components/blocks/ApprovalCard";
import { SessionStateBadge } from "@/components/SessionStateBadge";
import { getSessionState } from "@/hooks/useSessionState";
import {
  FEED_DEGRADED_LABELS,
  isFeedStale,
  monitorFeedErrorMessage,
  useMonitorFeed,
  type MonitorSession,
  type MonitorStatus,
} from "@/hooks/useMonitorFeed";
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
 * offline would invent a fact the server explicitly declined to assert. Only
 * `false` is a confirmed-offline runner.
 */
function livenessText(online: boolean | null): { text: string; tone: "unknown" | "up" | "down" } {
  if (online === null) return { text: "desconhecido", tone: "unknown" };
  return online ? { text: "online", tone: "up" } : { text: "offline", tone: "down" };
}

/** Phrase a degraded slug, falling back to the raw slug for ones we don't know. */
function degradedText(slug: string): string {
  return FEED_DEGRADED_LABELS[slug] ?? slug;
}

/** Wall-clock ms, re-read on an interval so age-based staleness can surface. */
function useNow(intervalMs: number, override?: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (override !== undefined) return;
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs, override]);
  return override ?? now;
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
  /** Frozen clock, for tests that need a deterministic staleness age. */
  nowMs?: number;
}

export function HudPanel({
  hostId = null,
  enabled = true,
  onExpandedChange = setHudExpanded,
  nowMs,
}: HudPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [responded, setResponded] = useState<Record<string, "accept" | "decline">>({});
  const [resolveErrors, setResolveErrors] = useState<Record<string, string>>({});
  const query = useMonitorFeed({ hostId, enabled });
  const feed = query.data ?? null;
  const now = useNow(1_000, nowMs);

  const pollFailing = query.isError;
  // A read that never landed and a feed that reports itself unbuildable are
  // the same thing: we do not know what is running.
  const unreadable = (feed === null && pollFailing) || feed?.unreadable === true;
  // No snapshot yet — booting, or disabled while identity resolves. Falling
  // through to counts here would paint an all-clear we have not earned.
  const loading = feed === null && !pollFailing;
  const stale = isFeedStale({
    lastSuccessAt: feed === null ? null : query.dataUpdatedAt,
    now,
    pollFailing,
  });
  const staleSeconds = Math.max(0, Math.round((now - query.dataUpdatedAt) / 1000));
  const failureReason = pollFailing ? monitorFeedErrorMessage(query.error) : null;
  // ANY slug counts — a slug this build has never heard of is still the
  // server telling us something went wrong.
  const degradedSlugs = feed?.degraded ?? [];

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    onExpandedChange(next);
  };

  const makeSubmit = (resolveSessionId: string): SubmitApprovalFn => {
    return (elicitationId, action, content) => {
      setResolveErrors((prev) => {
        const next = { ...prev };
        delete next[elicitationId];
        return next;
      });
      setResponded((prev) => ({ ...prev, [elicitationId]: action }));
      void approve(
        resolveSessionId,
        elicitationId,
        content === undefined ? { action } : { action, content },
      ).then(
        () => query.refetch(),
        (error: unknown) => {
          // Roll the card back to pending so the buttons return and the user
          // can retry — AND say what happened. A verdict that silently
          // evaporated would leave an agent blocked with the HUD implying it
          // had been answered.
          setResponded((prev) => {
            const next = { ...prev };
            delete next[elicitationId];
            return next;
          });
          setResolveErrors((prev) => ({
            ...prev,
            [elicitationId]:
              error instanceof Error && error.message
                ? error.message
                : "A resposta não pôde ser enviada.",
          }));
        },
      );
    };
  };

  let pill: string;
  if (unreadable) pill = "Feed indisponível";
  else if (loading || feed === null) pill = "Carregando…";
  else if (feed.counts === null) pill = "Contagens ilegíveis";
  else {
    const parts = [`${feed.counts.active} ativas`, `${feed.counts.awaiting} aguardando`];
    // Unknown-status and cap-omitted sessions are real sessions the tallies
    // above don't describe. Printing only the two clean numbers would present
    // a partial answer as a complete one.
    if (feed.counts.unknown > 0) parts.push(`${feed.counts.unknown} desconhecidas`);
    if (feed.counts.omitted > 0) parts.push(`+${feed.counts.omitted} omitidas`);
    if (feed.truncated || feed.countsPartial) parts.push("parcial");
    pill = parts.join(" · ");
  }

  return (
    <div
      data-testid="hud-panel"
      data-expanded={expanded}
      data-stale={stale}
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
        ) : stale ? (
          <ClockAlertIcon className="size-4 shrink-0 text-warning" aria-hidden />
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
        <span
          className={cn("min-w-0 flex-1 truncate font-medium", stale && "text-muted-foreground")}
        >
          {pill}
        </span>
        {stale && (
          <span data-testid="hud-stale" className="shrink-0 text-[11px] text-warning">
            desatualizado · {staleSeconds}s
          </span>
        )}
      </button>

      {expanded && (
        <div data-testid="hud-body" className="flex flex-col gap-2 overflow-y-auto px-3 pb-3">
          {unreadable && (
            <p data-testid="hud-unreadable" className="text-xs text-warning">
              {failureReason ?? "O feed de sessões não pôde ser montado."} O que está rodando é
              desconhecido — isto não quer dizer que nada está rodando.
            </p>
          )}
          {stale && !unreadable && (
            <p data-testid="hud-stale-detail" className="text-xs text-warning">
              Números de {staleSeconds}s atrás. {failureReason ?? "A atualização parou."} Podem já
              não valer.
            </p>
          )}
          {feed?.counts === null && !unreadable && (
            <p data-testid="hud-counts-unreadable" className="text-xs text-warning">
              As contagens do feed não puderam ser lidas — quantas sessões estão ativas ou
              aguardando é desconhecido.
            </p>
          )}
          {feed?.truncated && (
            <p data-testid="hud-truncated" className="text-xs text-muted-foreground">
              Lista parcial: nem toda sessão que casou está aqui
              {feed.counts && feed.counts.omitted > 0 ? ` (${feed.counts.omitted} omitidas)` : ""}.
            </p>
          )}
          {degradedSlugs.length > 0 && (
            <ul data-testid="hud-degraded" className="flex flex-col gap-0.5 text-xs text-warning">
              {degradedSlugs.map((slug) => (
                <li key={slug} data-slug={slug}>
                  {degradedText(slug)}
                </li>
              ))}
            </ul>
          )}
          {/* "Nothing running" is a claim about a feed we actually READ, whose
              counts parsed and which reported no degradation — never a
              stand-in for one we couldn't read. */}
          {feed &&
            !feed.unreadable &&
            feed.counts !== null &&
            feed.sessions.length === 0 &&
            !feed.truncated &&
            degradedSlugs.length === 0 && (
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
            {/* Server order = human-need order. Do not sort. */}
            {(feed?.sessions ?? []).map((session) => (
              <HudSessionRow
                key={session.sessionId}
                session={session}
                responded={responded}
                resolveErrors={resolveErrors}
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
  resolveErrors: Record<string, string>;
  makeSubmit: (resolveSessionId: string) => SubmitApprovalFn;
}

function HudSessionRow({ session, responded, resolveErrors, makeSubmit }: HudSessionRowProps) {
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
  const resolveError = prompt ? resolveErrors[prompt.id] : undefined;

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
            parcial: {session.degraded.map(degradedText).join(", ")}
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
      {resolveError && (
        <p data-testid="hud-resolve-error" role="alert" className="text-xs text-destructive">
          A resposta não foi enviada: {resolveError.replace(/\.$/, "")}. Tente novamente.
        </p>
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
