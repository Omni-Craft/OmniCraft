import { useEffect, useMemo, useRef, useState } from "react";

import { useChatStore } from "@/store/chatStore";

import { deriveMascotState, phaseFromBlockType, type MascotState } from "./mascotState";

/**
 * Read the live chat state and derive how the mascot should look right now.
 *
 * Everything comes from the chat store except the brief "just finished" beat:
 * a lingering `completed` response would keep the fish smiling for ever, so we
 * hold the celebration for a moment when a turn goes from active to done and
 * then let it settle.
 *
 * @param opts.offline Whether the session's runner/host is offline.
 */
export function useMascotState({ offline = false }: { offline?: boolean } = {}): MascotState {
  const status = useChatStore((s) => s.status);
  const sessionStatus = useChatStore((s) => s.sessionStatus);
  const activeResponse = useChatStore((s) => s.activeResponse);
  const lastBlockType = useChatStore((s) => s.blocks[s.blocks.length - 1]?.type);

  const active =
    status === "streaming" ||
    sessionStatus === "running" ||
    sessionStatus === "waiting" ||
    sessionStatus === "launching";

  // Celebrate for a beat when a turn finishes cleanly (active → not active,
  // without a failure), then clear so the fish settles back to idle.
  const [justFinished, setJustFinished] = useState(false);
  const wasActive = useRef(false);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (wasActive.current && !active && sessionStatus !== "failed") {
      setJustFinished(true);
      timer = setTimeout(() => setJustFinished(false), 2600);
    }
    wasActive.current = active;
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [active, sessionStatus]);

  return useMemo(
    () =>
      deriveMascotState({
        streaming: status === "streaming",
        sessionStatus,
        phase: phaseFromBlockType(lastBlockType),
        activeResponseState: activeResponse?.state ?? null,
        offline,
        justFinished,
      }),
    [status, sessionStatus, lastBlockType, activeResponse, offline, justFinished],
  );
}
