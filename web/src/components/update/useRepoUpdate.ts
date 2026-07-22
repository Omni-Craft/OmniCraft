import { useQuery } from "@tanstack/react-query";

import { hostFetch } from "@/lib/host";

/** Shape of `GET /api/update-status`. */
export interface UpdateStatus {
  running_commit: string | null;
  current_commit: string | null;
  update_available: boolean;
}

const OFF: UpdateStatus = {
  running_commit: null,
  current_commit: null,
  update_available: false,
};

/**
 * Read `/api/update-status`, resolving any failure to "no update".
 *
 * The banner this feeds prompts a restart, so a failed or malformed read must
 * never read as `update_available: true` — the same asymmetry the server keeps.
 */
async function fetchUpdateStatus(): Promise<UpdateStatus> {
  try {
    const res = await hostFetch("/api/update-status");
    if (!res.ok) return OFF;
    const data = (await res.json()) as Partial<UpdateStatus>;
    return {
      running_commit: typeof data.running_commit === "string" ? data.running_commit : null,
      current_commit: typeof data.current_commit === "string" ? data.current_commit : null,
      update_available: data.update_available === true,
    };
  } catch {
    // A poll that can't reach the server is not evidence of an update.
    return OFF;
  }
}

/**
 * Poll whether the checkout moved ahead of the running server.
 *
 * Polls on a slow cadence (60s): an update lands when someone pulls or
 * commits, not second to second, and the banner is a nudge, not an alarm.
 */
export function useRepoUpdate(): UpdateStatus {
  const { data } = useQuery({
    queryKey: ["repo-update-status"],
    queryFn: fetchUpdateStatus,
    refetchInterval: 60_000,
    // A tab left open overnight should notice an update when refocused.
    refetchOnWindowFocus: true,
    staleTime: 30_000,
    placeholderData: (prev) => prev ?? OFF,
  });
  return data ?? OFF;
}
