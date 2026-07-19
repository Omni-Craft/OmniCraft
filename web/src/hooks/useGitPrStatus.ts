// TanStack Query hook for `GET /v1/sessions/{id}/git-status` — the git +
// pull-request state of a session's workspace, as shown by the status bar
// above the composer.
//
// The endpoint always answers 200: a session with no workspace, a workspace
// that is not a git repository, a detached HEAD and a branch without an
// upstream are all all-`null` answers rather than failures. `error` is set
// only when git itself failed.

import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "@/lib/identity";
import { useChatStore } from "@/store/chatStore";

/** Change-set size of the workspace against its base branch. */
export interface GitDiffStat {
  added: number;
  removed: number;
  files: number;
}

/** A pull request opened from the workspace's current branch. */
export interface GitPullRequest {
  number: number;
  title: string;
  state: "open" | "merged" | "closed";
  /**
   * Aggregate CI state. `null` means "not known" — no checks reported, or
   * the server's per-request CI lookup budget ran out. Never a failure.
   */
  ci_status: "success" | "failure" | "pending" | null;
  url: string;
}

/** Body of `POST /v1/sessions/{id}/pull-request`. */
export interface SessionPullRequest {
  object: "session.pull_request";
  session_id: string;
  number: number;
  url: string;
  /** `false` when the branch already had an open PR — not an error. */
  created: boolean;
  title: string;
}

/** Body of `GET /v1/sessions/{id}/git-status`. */
export interface GitPrStatus {
  object: "session.git_status";
  session_id: string;
  /** Absolute workspace path; `null` when the session has no workspace. */
  workspace: string | null;
  /** Checked-out branch; `null` on a detached HEAD or outside a repo. */
  branch: string | null;
  /** Upstream ref of the branch, e.g. `"origin/main"`. */
  base_branch: string | null;
  ahead: number | null;
  behind: number | null;
  diff: GitDiffStat | null;
  /** `owner/name` of the workspace's GitHub remote; `null` when there is none. */
  repo_slug: string | null;
  prs: GitPullRequest[];
  /** Git failure reason; `null` on success. */
  error: string | null;
}

const QUERY_KEY = "session-git-status";

/** Poll cadence while the tab is visible. Slow — this is ambient status. */
const POLL_MS = 20_000;

async function fetchGitPrStatus(sessionId: string): Promise<GitPrStatus> {
  const res = await authenticatedFetch(`/v1/sessions/${encodeURIComponent(sessionId)}/git-status`);
  if (!res.ok) {
    throw new Error(`git status fetch failed: HTTP ${res.status}`);
  }
  const body = (await res.json()) as GitPrStatus;
  return { ...body, prs: body.prs ?? [] };
}

/**
 * Refetch once when the focused session's turn ends.
 *
 * The agent's last commits and file writes land at the end of a turn, so a
 * trailing refetch shows them without waiting out the poll interval.
 */
function useRefetchOnTurnEnd(sessionId: string | null | undefined) {
  const queryClient = useQueryClient();
  const focusedId = useChatStore((s) => s.conversationId);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const active =
    !!sessionId &&
    sessionId === focusedId &&
    (sessionStatus === "running" || sessionStatus === "waiting");
  const prev = useRef<{ id: string | null | undefined; active: boolean }>({
    id: sessionId,
    active,
  });

  useEffect(() => {
    const wentIdle = prev.current.id === sessionId && prev.current.active && !active;
    prev.current = { id: sessionId, active };
    if (wentIdle && sessionId) {
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY, sessionId] });
    }
  }, [sessionId, active, queryClient]);
}

/**
 * Read the git + PR status of a session's workspace.
 *
 * Polls slowly, and only while the tab is visible — the bar is ambient
 * context, not something worth waking a backgrounded tab for. Invalidated
 * once whenever the session's turn ends.
 *
 * @param sessionId Session/conversation id. Nullish disables the query.
 */
export function useGitPrStatus(sessionId: string | null | undefined) {
  useRefetchOnTurnEnd(sessionId);
  return useQuery({
    queryKey: [QUERY_KEY, sessionId],
    queryFn: () => fetchGitPrStatus(sessionId as string),
    enabled: !!sessionId,
    staleTime: POLL_MS,
    refetchInterval: () =>
      typeof document !== "undefined" && document.visibilityState === "hidden" ? false : POLL_MS,
    refetchIntervalInBackground: false,
    // No `placeholderData` carrying the previous query's data: the key is
    // per-session, so it would answer a freshly-opened session with the
    // branch/PRs of the one before it — and make the composer hide its
    // branch on the strength of another session's workspace. Within one
    // session the cache already serves the last body while refetching.
  });
}

/**
 * Reason a pull-request attempt was refused, as an `Error`.
 *
 * Every refusal is a 4xx whose `error.message` is already written for the
 * person reading it ("branch not pushed", "token without write access"), so
 * it is surfaced verbatim; the status code is the fallback for a body that
 * isn't the documented shape.
 */
async function pullRequestError(res: Response): Promise<Error> {
  let message = `HTTP ${res.status}`;
  try {
    const body = (await res.json()) as { error?: { message?: string } };
    if (body?.error?.message) message = body.error.message;
  } catch {
    // Non-JSON body — the status line is all there is to report.
  }
  return new Error(message);
}

/**
 * Open a pull request for the session's branch: `POST
 * /v1/sessions/{id}/pull-request`.
 *
 * The answer already carries the PR's number and URL, so it is written into
 * the git-status cache before the invalidation refetch — the bar shows the
 * PR on the click, not on the next poll. An existing PR comes back with
 * `created: false` and lands in the same place.
 *
 * @param sessionId Session/conversation id.
 */
export function useCreateSessionPullRequest(sessionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (): Promise<SessionPullRequest> => {
      const res = await authenticatedFetch(
        `/v1/sessions/${encodeURIComponent(sessionId)}/pull-request`,
        { method: "POST" },
      );
      if (!res.ok) throw await pullRequestError(res);
      return (await res.json()) as SessionPullRequest;
    },
    onSuccess: (result) => {
      queryClient.setQueryData([QUERY_KEY, sessionId], (prev: GitPrStatus | undefined) =>
        prev
          ? {
              ...prev,
              prs: [
                {
                  number: result.number,
                  title: result.title,
                  state: "open" as const,
                  ci_status: null,
                  url: result.url,
                },
                ...prev.prs.filter((item) => item.number !== result.number),
              ],
            }
          : prev,
      );
      queryClient.invalidateQueries({ queryKey: [QUERY_KEY, sessionId] });
    },
  });
}
