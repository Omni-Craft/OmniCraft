import type { MascotPose } from "./FuchoMascot";
import type { SessionStatus } from "@/lib/types";

// Maps what the chat is doing to how the mascot should look. Pure, so the
// mapping is testable and lives in one place — nothing is chosen at random:
// every pose corresponds to a real signal from the turn.

/** The coarse phase of the current stream, read from its latest block. */
export type MascotPhase = "reasoning" | "tool" | "text" | "error" | null;

/**
 * Classify a stream block's `type` into a mascot phase.
 *
 * @param type The `type` discriminant of the latest streaming block.
 */
export function phaseFromBlockType(type: string | undefined | null): MascotPhase {
  if (!type) return null;
  if (type === "error") return "error";
  if (type.startsWith("reasoning")) return "reasoning";
  if (type === "tool_group" || type === "tool_result" || type === "native_tool") return "tool";
  if (type === "text_chunk" || type === "text_done") return "text";
  return null;
}

export interface MascotSignals {
  /** Whether a response is actively streaming (`status === "streaming"`). */
  streaming: boolean;
  /** The session's status enum. */
  sessionStatus: SessionStatus;
  /** The phase of the latest streaming block, if any. */
  phase: MascotPhase;
  /** The active response's state, if there is one. */
  activeResponseState: "streaming" | "completed" | "cancelled" | "failed" | "incomplete" | null;
  /** True when the session's runner/host is offline. */
  offline: boolean;
  /** Brief window right after a successful finish, to hold the happy beat. */
  justFinished: boolean;
}

/** The mascot's pose plus, on key beats, the star buddy's pose. */
export interface MascotState {
  pose: MascotPose;
  starPose: MascotPose | null;
}

/**
 * Derive the mascot's pose (and the star's, on key beats) from the turn.
 *
 * Priority: an error outranks everything (the fish falls, the star reacts);
 * then, while active, the pose follows the phase — reasoning→pensando,
 * tool→codando, text→frente; a fresh success celebrates (both happy); offline
 * looks lost; otherwise it rests idle.
 *
 * @param s The current chat signals.
 */
export function deriveMascotState(s: MascotSignals): MascotState {
  if (s.phase === "error" || s.sessionStatus === "failed" || s.activeResponseState === "failed") {
    return { pose: "erro", starPose: "pensando" };
  }

  const active =
    s.streaming ||
    s.sessionStatus === "running" ||
    s.sessionStatus === "waiting" ||
    s.sessionStatus === "launching";

  if (active) {
    if (s.phase === "reasoning") return { pose: "pensando", starPose: null };
    if (s.phase === "tool") return { pose: "codando", starPose: null };
    if (s.phase === "text") return { pose: "frente", starPose: null };
    // Active but the first block hasn't landed yet — read as thinking.
    return { pose: "pensando", starPose: null };
  }

  // Celebrate only on the transient `justFinished` beat, not on the lingering
  // `completed` state — otherwise the fish would stay happy until the next turn.
  if (s.justFinished) {
    return { pose: "feliz", starPose: "feliz" };
  }

  if (s.offline) return { pose: "pensando", starPose: null };

  return { pose: "idle", starPose: null };
}
