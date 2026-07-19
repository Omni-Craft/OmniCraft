// TanStack Query hook for `GET /v1/sessions/{id}/git-status` — the git +
// pull-request state of a session's workspace, as shown by the status bar
// above the composer.
//
// The endpoint always answers 200: a session with no workspace, a workspace
// that is not a git repository, a detached HEAD and a branch without an
// upstream are all all-`null` answers rather than failures. `error` is set
// only when git itself failed.

import { useEffect, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

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
  /** Aggregate CI state; `null` when no checks reported. */
  ci_status: "success" | "failure" | "pending" | null;
  url: string;
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
    placeholderData: (prev) => prev,
  });
}
