// The monitor feed (`GET /v1/monitor/sessions`) — the single answer to "what
// is running, and what needs me" that the floating HUD polls.
//
// The payload is treated as UNTRUSTED BY CONSTRUCTION. Everything arriving off
// the wire crosses exactly one strict edge — `parseMonitorFeed` — and past that
// edge the rest of the app reads plain typed values and never re-checks them.
// The edge holds one rule: **a field we could not read is not a field that says
// "nothing".** A missing or malformed required field never gets a calm default;
// it becomes an explicit unknown plus a recorded degradation.
//
// Degrading and marking the tallies a floor are ONE operation here (see
// `FeedFaults.note`), mirroring the server's own accumulator: there is no path
// that can record a failure and forget the flag saying the numbers are a floor.
// Rows go through the SAME accumulator (`FeedFaults.row`), so a single row we
// could not fully resolve is enough to stop the pill presenting its counts as
// a total — an envelope that parsed cleanly says nothing about the rows in it.
//
// Concretely:
//
//   * `null` liveness / cost means UNKNOWN, never "offline" / "$0". Only
//     `false` is a confirmed-offline runner.
//   * `pending_elicitations_count` is `int | null` on the wire — `null` is an
//     unreadable prompt index, NEVER "nobody is waiting". It stays `null` here
//     and the UI must show an unknown, never a `0`.
//   * An unrecognized `status` becomes `"unknown"` rather than being forced
//     into the enum — a server that grows a new lifecycle state degrades
//     instead of breaking.
//   * `counts` that we cannot fully validate becomes `null`, not `{0, 0}`.
//     A HUD may not print "0 aguardando" off a payload it failed to parse.
//   * `counts.partial` means the tallies are a FLOOR, not a total. The server
//     sets it on any degradation; the client also sets it on any degradation it
//     detects itself.
//   * ANY slug in `degraded` — including one this build has never heard of —
//     marks the feed degraded. New server slugs must not be silently ignored
//     by an old client.
//   * `degraded: ["internal_error"]` means the feed could not be BUILT at all;
//     the route answers `200` with empty `sessions` in that case, so an empty
//     list is never read as calm.
//   * `truncated` means the response doesn't carry every matching session
//     (scan cut, or the row cap dropped rows — `counts.omitted`). Unreadable,
//     it reads as `true`: not knowing whether the list is complete is not the
//     same as knowing it is.

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

/** Feed-wide degraded slug meaning the server could not build the feed. */
export const FEED_UNREADABLE = "internal_error";

/**
 * Client-side slug: the envelope did not match the contract, so we cannot even
 * enumerate what is running. Not on the wire — it names OUR failure to read,
 * as distinct from the server's failure to build.
 */
export const ENVELOPE_UNREADABLE = "envelope_unreadable";

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
  pending_elicitations_unknown: "aprovações pendentes desta sessão não puderam ser lidas",
  pending_elicitation_unreadable: "o conteúdo da aprovação pendente não pôde ser lido",
  attention_rescue_unavailable: "sessões bloqueadas fora da varredura não puderam ser resgatadas",
  attention_rescue_truncated: "o resgate de sessões bloqueadas foi cortado",
  status_unknown: "o estado desta sessão não está registrado",
  status_unreadable: "o estado desta sessão não pôde ser lido",
  cost_unreadable: "o custo não pôde ser lido",
  counts_unreadable: "as contagens do feed não puderam ser lidas",
  generated_at_unreadable: "a hora do feed não pôde ser lida",
  host_id_unreadable: "não dá para saber a que host estas contagens se referem",
  session_labels_unreadable: "parte da identificação desta sessão não pôde ser lida",
  updated_at_unreadable: "a hora da última atividade desta sessão não pôde ser lida",
  truncated_unreadable: "não dá para saber se a lista está completa",
  degraded_unreadable: "a lista de falhas de uma sessão não pôde ser lida",
  [ENVELOPE_UNREADABLE]: "a resposta do feed não pôde ser lida",
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
  /**
   * Outstanding approval prompts, or `null` when the prompt index could not be
   * read. `null` is NOT `0`: the session may or may not be blocked on a human,
   * and only the server knows. Surfaces must render it as an unknown.
   */
  pendingElicitationsCount: number | null;
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
  /**
   * Matching sessions not carried in `sessions`: rows the cap dropped, plus any
   * session with pending attention the server could not resolve into a row.
   */
  omitted: number;
  /** These tallies are a FLOOR, not a total — something went unresolved. */
  partial: boolean;
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
  /** `true` also when we could not tell: an unknown list length is not a full one. */
  truncated: boolean;
  /** Every degraded slug, known or not, feed-wide. Empty means fully resolved. */
  degraded: string[];
  /**
   * Nothing about what is running could be read — the server said
   * `internal_error`, or the envelope did not parse. `sessions` being empty
   * then says NOTHING about what is running.
   */
  unreadable: boolean;
  /** The tallies are a floor, not a total (server `counts.partial`, or our own). */
  countsPartial: boolean;
}

/**
 * Every failure recorded on one read, envelope and rows alike.
 *
 * `note()` records the slug AND makes the tallies a floor — one operation, so
 * no branch can degrade the feed while still presenting its counts as
 * complete. `fail()` adds that we cannot enumerate at all. The list is
 * read-only from outside: `note()` is the only way in, which is what keeps a
 * degradation from being recorded without its companion flag.
 */
class FeedFaults {
  #slugs: string[] = [];
  #fatal = false;

  get slugs(): string[] {
    return [...this.#slugs];
  }

  get fatal(): boolean {
    return this.#fatal;
  }

  note(slug: string): void {
    if (!this.#slugs.includes(slug)) this.#slugs.push(slug);
  }

  /**
   * A row's own failures. They land on the row AND on the feed, because a row
   * we could not fully resolve is a session the tallies cannot claim to
   * describe — the pill must read as a floor even when the envelope is clean.
   */
  row(): RowFaults {
    return new RowFaults(this);
  }

  /** A failure that leaves us unable to say what is running. */
  fail(slug: string): void {
    this.note(slug);
    this.#fatal = true;
  }

  /** Anything recorded means the tallies are a floor. */
  get partial(): boolean {
    return this.#slugs.length > 0;
  }
}

/**
 * One row's failures. There is no way to record one here without it reaching
 * the feed's accumulator, so a degraded row can never sit inside a feed whose
 * pill still presents its counts as a total.
 */
class RowFaults {
  #slugs: string[] = [];
  #feed: FeedFaults;

  constructor(feed: FeedFaults) {
    this.#feed = feed;
  }

  get slugs(): string[] {
    return [...this.#slugs];
  }

  note(slug: string): void {
    if (!this.#slugs.includes(slug)) this.#slugs.push(slug);
    this.#feed.note(slug);
  }
}

/**
 * Anything that can record a failure — the feed's accumulator or a row's.
 *
 * Every field reader below takes one, so producing a `null` from an unreadable
 * value without recording it is not something this file can express.
 */
interface Faults {
  note(slug: string): void;
}

/** A field read: a trusted value, or "not readable" — never a quiet default. */
type Read<T> = { ok: true; value: T } | { ok: false };

const UNREADABLE: Read<never> = { ok: false };
const readable = <T>(value: T): Read<T> => ({ ok: true, value });

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function readBool(value: unknown): Read<boolean> {
  return typeof value === "boolean" ? readable(value) : UNREADABLE;
}

function readString(value: unknown): Read<string> {
  return typeof value === "string" ? readable(value) : UNREADABLE;
}

function readNumber(value: unknown): Read<number> {
  return typeof value === "number" && Number.isFinite(value) ? readable(value) : UNREADABLE;
}

/** A tally: whole and non-negative. `-1` is out of domain, not a small number. */
function readCount(value: unknown): Read<number> {
  return typeof value === "number" && Number.isInteger(value) && value >= 0
    ? readable(value)
    : UNREADABLE;
}

function readStringArray(value: unknown): Read<string[]> {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string")
    ? readable(value as string[])
    : UNREADABLE;
}

function readArray(value: unknown): Read<unknown[]> {
  return Array.isArray(value) ? readable(value) : UNREADABLE;
}

/**
 * Read a field the contract allows to be absent or `null`.
 *
 * The distinction that matters: ABSENT is a fact (the server had nothing to
 * say), while PRESENT-BUT-UNREADABLE is a failure (the server said something we
 * could not understand). Both come back `null` — they have to, we have no
 * value — but only the second records a fault, so a row that lost information
 * cannot sit inside a feed still presenting its counts as a total.
 *
 * The accumulator is a required parameter for exactly that reason: a caller
 * cannot get a `null` out of a malformed value without one.
 */
function optional<T>(
  raw: Record<string, unknown>,
  key: string,
  read: (value: unknown) => Read<T>,
  faults: Faults,
  slug: string,
): T | null {
  const value = raw[key];
  if (value === undefined || value === null) return null;
  const parsed = read(value);
  if (parsed.ok) return parsed.value;
  faults.note(slug);
  return null;
}

/**
 * Read a field the contract requires. Absent is unreadable here — there is no
 * legitimate "not applicable" for a field that must be there.
 */
function required<T>(
  raw: Record<string, unknown>,
  key: string,
  read: (value: unknown) => Read<T>,
  faults: Faults,
  slug: string,
): Read<T> {
  const parsed = read(raw[key]);
  if (!parsed.ok) faults.note(slug);
  return parsed;
}

/**
 * Map a wire `status` onto the enum. Anything unrecognized — a newer server,
 * a corrupt row — becomes `"unknown"`, which renders as an honest unknown
 * rather than being dropped or defaulted to `"idle"`.
 */
export function normalizeStatus(value: unknown): MonitorStatus {
  return KNOWN_STATUSES.find((s) => s === value) ?? "unknown";
}

/** A wire status, or unreadable — a value outside the enum is not a status. */
function readStatus(value: unknown): Read<MonitorStatus> {
  const status = KNOWN_STATUSES.find((s) => s === value);
  return status === undefined ? UNREADABLE : readable(status);
}

/**
 * Parse the tallies, or return `null` when they cannot be trusted.
 *
 * Every field of the contract is required and must be a whole non-negative
 * number (`partial`, a boolean). Anything else means we do not know the
 * tallies — and `null` is the only honest way to say that. Returning
 * `{active: 0, awaiting: 0}` off a partial body would let an incomplete
 * payload render as "nothing needs you".
 *
 * This is the one place that reads fields without an accumulator, and it is
 * safe for a structural reason: nothing here is optional, so there is no
 * per-field `null` to leak — one bad field collapses the whole object, and the
 * caller records that.
 */
export function parseMonitorCounts(value: unknown): MonitorCounts | null {
  const raw = asRecord(value);
  if (raw === null) return null;
  const active = readCount(raw.active);
  const awaiting = readCount(raw.awaiting);
  const unknown = readCount(raw.unknown);
  const omitted = readCount(raw.omitted);
  const partial = readBool(raw.partial);
  if (!active.ok || !awaiting.ok || !unknown.ok || !omitted.ok || !partial.ok) return null;
  return {
    active: active.value,
    awaiting: awaiting.value,
    unknown: unknown.value,
    omitted: omitted.value,
    partial: partial.value,
  };
}

/** The slug for anything unreadable inside a parked prompt. */
const PROMPT_UNREADABLE = "pending_elicitation_unreadable";

function parsePendingElicitation(value: unknown, faults: Faults): MonitorPendingElicitation | null {
  const raw = asRecord(value);
  if (raw === null) return null;
  const id = required(raw, "id", readString, faults, PROMPT_UNREADABLE);
  const sessionId = required(raw, "session_id", readString, faults, PROMPT_UNREADABLE);
  if (!id.ok || !sessionId.ok) return null;
  return {
    id: id.value,
    sessionId: sessionId.value,
    // Absent `kind` is the contract's own default; a `kind` of the wrong shape
    // is information we lost, and lands as a fault rather than as that default.
    kind: optional(raw, "kind", readString, faults, PROMPT_UNREADABLE) ?? "unknown",
    summary: optional(raw, "summary", readString, faults, PROMPT_UNREADABLE),
  };
}

/**
 * Parse one row. Returns `null` only when the row cannot be identified at all —
 * the caller then records it as a dropped row rather than a shorter list.
 */
function parseSession(value: unknown, faults: FeedFaults): MonitorSession | null {
  const raw = asRecord(value);
  if (raw === null) return null;

  // One accumulator for the whole row: every read below takes it, so no field
  // can degrade to `null` without the feed hearing about it. Slugs the SERVER
  // reported on the row go through it too — they are equally a reason the
  // tallies do not describe everything.
  const row = faults.row();
  const sessionId = required(raw, "session_id", readString, row, ROW_UNREADABLE);
  if (!sessionId.ok) return null;

  const wireDegraded = required(raw, "degraded", readStringArray, row, "degraded_unreadable");
  if (wireDegraded.ok) for (const slug of wireDegraded.value) row.note(slug);

  const status = required(raw, "status", readStatus, row, "status_unreadable");

  // `null` is the server saying it could not read the prompt index; a value
  // outside the domain (negative, fractional, a string) is the same unknown
  // reached our way. Neither may become a `0` — that would hide a session
  // blocked on a human behind a confident all-clear.
  const pendingCount = optional(
    raw,
    "pending_elicitations_count",
    readCount,
    row,
    "pending_elicitations_unknown",
  );
  if (pendingCount === null) row.note("pending_elicitations_unknown");

  // Absent is "nothing parked here"; present but unparseable is a prompt we
  // lost, which is the case the row must not render as calm.
  const prompt = optional(
    raw,
    "pending_elicitation",
    (field) => {
      const parsed = parsePendingElicitation(field, row);
      return parsed === null ? UNREADABLE : readable(parsed);
    },
    row,
    PROMPT_UNREADABLE,
  );

  const runnerOnline = optional(raw, "runner_online", readBool, row, "liveness_unavailable");
  const hostOnline = optional(raw, "host_online", readBool, row, "liveness_unavailable");
  const costUsd = optional(raw, "cost_usd", readNumber, row, "cost_unreadable");
  const updatedAt = optional(raw, "updated_at", readNumber, row, "updated_at_unreadable");

  // Labels assert nothing on their own, so an unreadable one still renders as
  // absent — but it is information the row lost, and the feed is told.
  const label = (key: string) => optional(raw, key, readString, row, "session_labels_unreadable");

  return {
    sessionId: sessionId.value,
    agentName: label("agent_name"),
    title: label("title"),
    project: label("project"),
    workspace: label("workspace"),
    status: status.ok ? status.value : "unknown",
    pendingElicitationsCount: pendingCount,
    pendingElicitation: prompt,
    runnerOnline,
    hostOnline,
    updatedAt,
    costUsd,
    degraded: row.slugs,
  };
}

function unreadableFeed(faults: FeedFaults, hostId: string | null = null): MonitorFeed {
  return {
    generatedAt: null,
    hostId,
    sessions: [],
    counts: null,
    truncated: true,
    degraded: faults.slugs,
    unreadable: true,
    countsPartial: true,
  };
}

/**
 * The single strict edge between the wire and the app. Never throws — and never
 * fabricates calm either: anything it could not read comes back as an explicit
 * unknown with the failure recorded. Past this function the HUD reads plain
 * typed values and does no re-checking.
 */
export function parseMonitorFeed(body: unknown): MonitorFeed {
  const faults = new FeedFaults();
  const raw = asRecord(body);
  if (raw === null) {
    faults.fail(ENVELOPE_UNREADABLE);
    return unreadableFeed(faults);
  }

  // The server's own failure list. Losing it means losing every reason the
  // feed might be incomplete, so an unreadable list is fatal, and `internal_error`
  // says the server never built the feed at all.
  const wireDegraded = required(raw, "degraded", readStringArray, faults, ENVELOPE_UNREADABLE);
  if (!wireDegraded.ok) faults.fail(ENVELOPE_UNREADABLE);
  else
    for (const slug of wireDegraded.value) {
      if (slug === FEED_UNREADABLE) faults.fail(slug);
      else faults.note(slug);
    }

  // Rows we cannot enumerate are not an empty account.
  const rawSessions = required(raw, "sessions", readArray, faults, ENVELOPE_UNREADABLE);
  if (!rawSessions.ok) faults.fail(ENVELOPE_UNREADABLE);
  const rows = rawSessions.ok ? rawSessions.value : [];
  const sessions = rows
    .map((row) => parseSession(row, faults))
    .filter((s): s is MonitorSession => s !== null);
  if (sessions.length !== rows.length) faults.note(ROW_UNREADABLE);

  const counts = required(
    raw,
    "counts",
    (value) => {
      const parsed = parseMonitorCounts(value);
      return parsed === null ? UNREADABLE : readable(parsed);
    },
    faults,
    "counts_unreadable",
  );

  const generatedAt = required(raw, "generated_at", readNumber, faults, "generated_at_unreadable");

  // Not knowing whether the list is complete is not the same as knowing it is.
  const truncated = required(raw, "truncated", readBool, faults, "truncated_unreadable");

  // Unfiltered feeds carry no `host_id` at all; one of the wrong shape means we
  // cannot say which host these numbers describe.
  const hostId = optional(raw, "host_id", readString, faults, "host_id_unreadable");
  if (faults.fatal) return unreadableFeed(faults, hostId);

  return {
    generatedAt: generatedAt.ok ? generatedAt.value : null,
    hostId,
    sessions,
    counts: counts.ok ? counts.value : null,
    truncated: truncated.ok ? truncated.value : true,
    degraded: faults.slugs,
    unreadable: false,
    countsPartial: !counts.ok || counts.value.partial || faults.partial,
  };
}

/**
 * A feed read that failed. Carries the status so the HUD can say WHY —
 * a rejected `host_id` (400/404) is a filter the user can fix, an
 * unverifiable one (503) is a server that cannot check, and none of them is
 * an empty account.
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
    // The feed itself always answers 200 (a failure to build it is reported
    // in the body). Every error status is about the `host_id` filter, and each
    // is a distinct, actionable failure — surfacing any of them as an empty
    // feed would hide a fixable mistake behind a clean board.
    if (res.status === 400 || res.status === 404) {
      throw new MonitorFeedError(
        `O filtro de host foi recusado pelo servidor (${res.status}). O feed não foi lido.`,
        res.status,
      );
    }
    if (res.status === 503) {
      throw new MonitorFeedError(
        "O servidor não tem registro de hosts para verificar esse filtro (503). O feed não foi lido.",
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
