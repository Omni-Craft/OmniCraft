// The monitor feed (`GET /v1/monitor/sessions`) — the single answer to "what
// is running, and what needs me" that the floating HUD polls.
//
// The server was built to never turn an absent answer into a clean one, and
// this module's whole job is to hold that line across the wire boundary. The
// rule it enforces everywhere: **a payload we could not read is not a payload
// that says "nothing".** So parsing is fail-CLOSED — anything malformed,
// missing or unrecognized degrades into an explicit unknown that the UI has to
// render as such, never into a zero.
//
// Concretely:
//
//   * `null` liveness / cost means UNKNOWN, never "offline" / "$0". Only
//     `false` is a confirmed-offline runner.
//   * An unrecognized `status` becomes `"unknown"` rather than being forced
//     into the enum — a server that grows a new lifecycle state degrades
//     instead of breaking.
//   * `counts` that we cannot fully validate becomes `null`, not `{0, 0}`.
//     A HUD may not print "0 aguardando" off a payload it failed to parse.
//   * ANY slug in `degraded` — including one this build has never heard of —
//     marks the feed degraded. New server slugs must not be silently ignored
//     by an old client.
//   * `degraded: ["internal_error"]` means the feed could not be BUILT at all;
//     `unreadable` names that case so an empty list is never read as calm.
//   * `truncated` means the response doesn't carry every matching session
//     (scan cut, or the row cap dropped rows — `counts.omitted`). The counts
//     still describe everything that matched unless `scan_truncated` says
//     even they are partial.

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** How often the HUD re-reads the feed. */
export const MONITOR_POLL_MS = 3_000;

/**
 * How long a successfully-read snapshot stays trustworthy. Four missed polls:
 * long enough that a single blip doesn't cry wolf, short enough that a HUD
 * silently frozen on old numbers gets called out fast.
 */
export const MONITOR_STALE_AFTER_MS = MONITOR_POLL_MS * 4;

/** Feed-wide degraded slug meaning the feed could not be built at all. */
export const FEED_UNREADABLE = "internal_error";

/**
 * Client-side slug for rows the client itself could not parse. Not on the
 * wire — it exists so a dropped row still shows up as a degradation instead
 * of shrinking the list silently.
 */
export const ROW_UNREADABLE = "row_unreadable";

/**
 * Feed-wide slugs this build knows how to phrase. Unknown slugs are still
 * treated as degradations (and shown raw) — this list only drives copy, never
 * the decision of whether something is wrong.
 */
export const FEED_DEGRADED_LABELS: Record<string, string> = {
  internal_error: "o feed não pôde ser montado",
  scan_truncated: "a varredura foi cortada — até as contagens são parciais",
  liveness_unavailable: "não foi possível verificar runners",
  liveness_partial: "alguns runners não puderam ser verificados",
  permissions_unavailable: "não foi possível conferir permissões",
  agent_names_unavailable: "nomes de agentes indisponíveis",
  child_sessions_unavailable: "sub-agentes não puderam ser lidos",
  pending_elicitations_unavailable: "aprovações pendentes não puderam ser lidas",
  attention_rescue_unavailable: "sessões bloqueadas fora da varredura não puderam ser resgatadas",
  attention_rescue_truncated: "o resgate de sessões bloqueadas foi cortado",
  host_unverified: "o host do filtro não pôde ser verificado",
  [ROW_UNREADABLE]: "alguma linha do feed não pôde ser lida",
};

/**
 * Lifecycle status of a monitored session. `"unknown"` is on the wire now (a
 * dispatched session with no status on record) AND the client's landing spot
 * for any value outside the enum.
 */
export type MonitorStatus = "idle" | "launching" | "running" | "waiting" | "failed" | "unknown";

const KNOWN_STATUSES: readonly MonitorStatus[] = [
  "idle",
  "launching",
  "running",
  "waiting",
  "failed",
  "unknown",
];

export interface MonitorPendingElicitation {
  id: string;
  /**
   * The session that owns the parked prompt — the resolve POST target. This
   * can be a sub-agent CHILD of the row it is reported on, so a verdict must
   * be posted here, not to the row's `sessionId`.
   */
  sessionId: string;
  kind: string;
  /** Human-readable one-liner, or `null` when the payload was unreadable. */
  summary: string | null;
}

export interface MonitorSession {
  sessionId: string;
  agentName: string | null;
  title: string | null;
  project: string | null;
  workspace: string | null;
  status: MonitorStatus;
  pendingElicitationsCount: number;
  pendingElicitation: MonitorPendingElicitation | null;
  /** `null` = unknown. Only `false` is a confirmed-offline runner. */
  runnerOnline: boolean | null;
  /** `null` = unknown (or no host bound). Only `false` is confirmed offline. */
  hostOnline: boolean | null;
  updatedAt: number | null;
  /** `null` = no cost recorded / unreadable, never `0`. */
  costUsd: number | null;
  degraded: string[];
}

/**
 * Tallies over every session that MATCHED — not just the rows carried. A
 * headline that shrank with the page would repeat the exact failure the feed
 * exists to avoid.
 */
export interface MonitorCounts {
  /** Doing something: launching / running / waiting / failed. Excludes unknown. */
  active: number;
  /** Blocked on a human. Counted independently of `active`. */
  awaiting: number;
  /** Status could not be resolved — neither active nor idle. */
  unknown: number;
  /** Matching sessions the row cap dropped: counted here, absent from `sessions`. */
  omitted: number;
}

export interface MonitorFeed {
  generatedAt: number | null;
  hostId: string | null;
  /**
   * Rows in the server's order: blocked first, then failed, then active work,
   * then unresolved, then idle. Already ranked by how much each needs a
   * human — surfaces must NOT re-sort.
   */
  sessions: MonitorSession[];
  /** `null` when the tallies could not be read — never assume zeros. */
  counts: MonitorCounts | null;
  truncated: boolean;
  /** Every degraded slug, known or not, feed-wide. Empty means fully resolved. */
  degraded: string[];
  /**
   * The feed itself could not be read — the server said `internal_error`, or
   * the payload did not parse. `sessions` being empty then says NOTHING about
   * what is running.
   */
  unreadable: boolean;
  /** Even the counts are partial (`scan_truncated`). */
  countsPartial: boolean;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** `null` for anything that isn't a real boolean — including a missing key. */
function boolOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

/** `null` for anything that isn't a finite number, so `0` stays meaningful. */
function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/**
 * Map a wire `status` onto the enum. Anything unrecognized — a newer server,
 * a corrupt row — becomes `"unknown"`, which renders as an honest unknown
 * rather than being dropped or defaulted to `"idle"`.
 */
export function normalizeStatus(value: unknown): MonitorStatus {
  return KNOWN_STATUSES.find((s) => s === value) ?? "unknown";
}

/**
 * Parse the tallies, or return `null` when they cannot be trusted.
 *
 * `active` and `awaiting` must be real non-negative numbers; `unknown` and
 * `omitted` may be absent (an older server) but must be numbers when present.
 * Anything else means we do not know the tallies — and `null` is the only
 * honest way to say that. Returning `{active: 0, awaiting: 0}` here would let
 * a malformed body render as "nothing needs you".
 */
export function parseMonitorCounts(value: unknown): MonitorCounts | null {
  const raw = asRecord(value);
  if (raw === null) return null;
  const read = (key: string, required: boolean): number | null => {
    if (raw[key] === undefined) return required ? null : 0;
    const n = numberOrNull(raw[key]);
    return n === null || n < 0 ? null : n;
  };
  const active = read("active", true);
  const awaiting = read("awaiting", true);
  const unknown = read("unknown", false);
  const omitted = read("omitted", false);
  if (active === null || awaiting === null || unknown === null || omitted === null) return null;
  return { active, awaiting, unknown, omitted };
}

function parsePendingElicitation(value: unknown): MonitorPendingElicitation | null {
  const raw = asRecord(value);
  if (raw === null) return null;
  const id = stringOrNull(raw.id);
  const sessionId = stringOrNull(raw.session_id);
  if (id === null || sessionId === null) return null;
  return {
    id,
    sessionId,
    kind: stringOrNull(raw.kind) ?? "unknown",
    summary: stringOrNull(raw.summary),
  };
}

function parseSession(value: unknown): MonitorSession | null {
  const raw = asRecord(value);
  if (raw === null) return null;
  const sessionId = stringOrNull(raw.session_id);
  if (sessionId === null) return null;
  const pendingCount = numberOrNull(raw.pending_elicitations_count);
  const rowDegraded = stringList(raw.degraded);
  return {
    sessionId,
    agentName: stringOrNull(raw.agent_name),
    title: stringOrNull(raw.title),
    project: stringOrNull(raw.project),
    workspace: stringOrNull(raw.workspace),
    status: normalizeStatus(raw.status),
    // An unreadable count is not "no prompts" — fall back to 0 but say the
    // row is degraded so nothing renders it as a clean, unblocked session.
    pendingElicitationsCount: pendingCount ?? 0,
    pendingElicitation: parsePendingElicitation(raw.pending_elicitation),
    runnerOnline: boolOrNull(raw.runner_online),
    hostOnline: boolOrNull(raw.host_online),
    updatedAt: numberOrNull(raw.updated_at),
    costUsd: numberOrNull(raw.cost_usd),
    degraded: pendingCount === null ? [...rowDegraded, ROW_UNREADABLE] : rowDegraded,
  };
}

/**
 * Turn a raw feed body into the client shape. Never throws — but never
 * fabricates calm either: a body that isn't a feed comes back `unreadable`
 * with `counts: null`.
 */
export function parseMonitorFeed(body: unknown): MonitorFeed {
  const raw = asRecord(body);
  if (raw === null) {
    return {
      generatedAt: null,
      hostId: null,
      sessions: [],
      counts: null,
      truncated: false,
      degraded: [FEED_UNREADABLE],
      unreadable: true,
      countsPartial: true,
    };
  }
  const degraded = [...stringList(raw.degraded)];
  // `sessions` absent or not an array is a payload we cannot read — not an
  // empty account.
  const rawSessions = raw.sessions;
  const sessionsUnreadable = !Array.isArray(rawSessions);
  const sessions = (Array.isArray(rawSessions) ? rawSessions : [])
    .map(parseSession)
    .filter((s): s is MonitorSession => s !== null);
  // Rows the client dropped are a degradation, not a shorter list.
  if (Array.isArray(rawSessions) && sessions.length !== rawSessions.length) {
    if (!degraded.includes(ROW_UNREADABLE)) degraded.push(ROW_UNREADABLE);
  }
  const counts = parseMonitorCounts(raw.counts);
  if (sessionsUnreadable && !degraded.includes(FEED_UNREADABLE)) degraded.push(FEED_UNREADABLE);
  return {
    generatedAt: numberOrNull(raw.generated_at),
    hostId: stringOrNull(raw.host_id),
    sessions,
    counts,
    truncated: raw.truncated === true,
    degraded,
    unreadable: degraded.includes(FEED_UNREADABLE),
    countsPartial: counts === null || degraded.includes("scan_truncated"),
  };
}

/**
 * A feed read that failed. Carries the status so the HUD can say WHY —
 * a rejected `host_id` (400/404) is a filter the user can fix, not a server
 * that is down, and neither is an empty account.
 */
export class MonitorFeedError extends Error {
  readonly status: number | null;
  constructor(message: string, status: number | null) {
    super(message);
    this.name = "MonitorFeedError";
    this.status = status;
  }
}

/** Human-readable reason for a failed read. */
export function monitorFeedErrorMessage(error: unknown): string {
  if (error instanceof MonitorFeedError) return error.message;
  return "Não foi possível ler o feed de sessões.";
}

/** Fetch one snapshot of the feed. */
export async function fetchMonitorFeed(hostId?: string | null): Promise<MonitorFeed> {
  const params = new URLSearchParams({ only_active: "true" });
  if (hostId) params.set("host_id", hostId);
  let res: Response;
  try {
    res = await authenticatedFetch(`/v1/monitor/sessions?${params.toString()}`);
  } catch {
    throw new MonitorFeedError("O servidor não respondeu.", null);
  }
  if (!res.ok) {
    // A rejected filter is a distinct, actionable failure — surfacing it as a
    // generic outage (or worse, an empty feed) would hide a fixable mistake.
    if (res.status === 400 || res.status === 404) {
      throw new MonitorFeedError(
        `O filtro de host foi recusado pelo servidor (${res.status}). O feed não foi lido.`,
        res.status,
      );
    }
    throw new MonitorFeedError(`O servidor respondeu ${res.status}.`, res.status);
  }
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    throw new MonitorFeedError("A resposta do servidor não pôde ser lida.", res.status);
  }
  return parseMonitorFeed(body);
}

/**
 * Whether what is on screen can still be called current.
 *
 * Age is measured from the CLIENT's last successful read, not from the feed's
 * `generated_at`: server clock skew would otherwise make a healthy HUD claim
 * to be stale (or, worse, a stale one claim to be fresh).
 */
export function isFeedStale({
  lastSuccessAt,
  now,
  pollFailing,
  staleAfterMs = MONITOR_STALE_AFTER_MS,
}: {
  lastSuccessAt: number | null;
  now: number;
  pollFailing: boolean;
  staleAfterMs?: number;
}): boolean {
  if (lastSuccessAt === null) return false; // nothing read yet — that's "loading", not "stale"
  if (pollFailing) return true;
  return now - lastSuccessAt > staleAfterMs;
}

export interface UseMonitorFeedOptions {
  hostId?: string | null;
  /** Held false until the viewer identity resolves in a fresh renderer. */
  enabled?: boolean;
}

/**
 * Poll the monitor feed. `staleTime: 0` so every tick is a real read — the
 * feed's whole value is being current.
 */
export function useMonitorFeed({ hostId = null, enabled = true }: UseMonitorFeedOptions = {}) {
  return useQuery({
    queryKey: ["monitor-feed", hostId],
    queryFn: () => fetchMonitorFeed(hostId),
    enabled,
    refetchInterval: MONITOR_POLL_MS,
    staleTime: 0,
    // A transient blip shouldn't blank the HUD; the previous snapshot stays
    // on screen while the retry runs — flagged as stale, never as current.
    retry: 1,
  });
}
