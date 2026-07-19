// The monitor feed (`GET /v1/monitor/sessions`) — the single answer to "what
// is running, and what needs me" that the floating HUD polls.
//
// Everything the server can't resolve arrives as an explicit unknown, and this
// module's whole job is to keep it that way across the wire boundary:
//
//   * `null` liveness / cost means UNKNOWN, never "offline" / "$0". The
//     parsers below never coalesce those to a value.
//   * An unrecognized `status` becomes `"unknown"` rather than being forced
//     into the enum (or crashing an icon lookup) — a server that grows a new
//     lifecycle state must degrade, not break.
//   * `degraded: ["internal_error"]` with no rows means the feed could not be
//     BUILT. `unreadable` names that case so no surface can mistake the empty
//     list for "nothing is running".
//   * `truncated` means the tallies describe the returned rows only.

import { useQuery } from "@tanstack/react-query";
import { authenticatedFetch } from "@/lib/identity";

/** How often the HUD re-reads the feed. */
export const MONITOR_POLL_MS = 3_000;

/** Feed-wide degraded slug meaning the feed could not be built at all. */
export const FEED_UNREADABLE = "internal_error";

/**
 * Lifecycle status of a monitored session. `"unknown"` is a CLIENT-side value
 * for anything the server sent that isn't in the documented enum — it is never
 * on the wire.
 */
export type MonitorStatus = "idle" | "launching" | "running" | "waiting" | "failed" | "unknown";

const KNOWN_STATUSES: readonly MonitorStatus[] = [
  "idle",
  "launching",
  "running",
  "waiting",
  "failed",
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
  /** `null` = unknown, never "offline". */
  runnerOnline: boolean | null;
  /** `null` = unknown (or no host bound), never "offline". */
  hostOnline: boolean | null;
  updatedAt: number;
  /** `null` = no cost recorded / unreadable, never `0`. */
  costUsd: number | null;
  degraded: string[];
}

export interface MonitorFeed {
  generatedAt: number;
  hostId: string | null;
  sessions: MonitorSession[];
  counts: { active: number; awaiting: number };
  truncated: boolean;
  degraded: string[];
  /**
   * The feed itself could not be read. When true, `sessions` being empty says
   * NOTHING about what is running — surfaces must report the failure instead
   * of an all-clear.
   */
  unreadable: boolean;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : {};
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
 * Map a wire `status` onto the client enum. Anything unrecognized (a newer
 * server, a degraded row) becomes `"unknown"` — rendered as an honest unknown
 * rather than dropped or defaulted to `"idle"`.
 */
export function normalizeStatus(value: unknown): MonitorStatus {
  return KNOWN_STATUSES.find((s) => s === value) ?? "unknown";
}

function parsePendingElicitation(value: unknown): MonitorPendingElicitation | null {
  const raw = asRecord(value);
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
  const sessionId = stringOrNull(raw.session_id);
  if (sessionId === null) return null;
  return {
    sessionId,
    agentName: stringOrNull(raw.agent_name),
    title: stringOrNull(raw.title),
    project: stringOrNull(raw.project),
    workspace: stringOrNull(raw.workspace),
    status: normalizeStatus(raw.status),
    pendingElicitationsCount: numberOrNull(raw.pending_elicitations_count) ?? 0,
    pendingElicitation: parsePendingElicitation(raw.pending_elicitation),
    runnerOnline: boolOrNull(raw.runner_online),
    hostOnline: boolOrNull(raw.host_online),
    updatedAt: numberOrNull(raw.updated_at) ?? 0,
    costUsd: numberOrNull(raw.cost_usd),
    degraded: stringList(raw.degraded),
  };
}

/** Turn a raw feed body into the client shape. Never throws on odd input. */
export function parseMonitorFeed(body: unknown): MonitorFeed {
  const raw = asRecord(body);
  const counts = asRecord(raw.counts);
  const degraded = stringList(raw.degraded);
  const sessions = (Array.isArray(raw.sessions) ? raw.sessions : [])
    .map(parseSession)
    .filter((s): s is MonitorSession => s !== null);
  return {
    generatedAt: numberOrNull(raw.generated_at) ?? 0,
    hostId: stringOrNull(raw.host_id),
    sessions,
    counts: {
      active: numberOrNull(counts.active) ?? 0,
      awaiting: numberOrNull(counts.awaiting) ?? 0,
    },
    truncated: raw.truncated === true,
    degraded,
    unreadable: degraded.includes(FEED_UNREADABLE),
  };
}

/** Fetch one snapshot of the feed. */
export async function fetchMonitorFeed(hostId?: string | null): Promise<MonitorFeed> {
  const params = new URLSearchParams({ only_active: "true" });
  if (hostId) params.set("host_id", hostId);
  const res = await authenticatedFetch(`/v1/monitor/sessions?${params.toString()}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return parseMonitorFeed(await res.json());
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
    // on screen while the retry runs.
    retry: 1,
  });
}
