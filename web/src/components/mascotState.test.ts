import { describe, expect, it } from "vitest";

import { deriveMascotState, phaseFromBlockType, type MascotSignals } from "./mascotState";

const base: MascotSignals = {
  streaming: false,
  sessionStatus: "idle",
  phase: null,
  activeResponseState: null,
  offline: false,
  justFinished: false,
};

describe("phaseFromBlockType", () => {
  it("classifies the streaming block types the mascot cares about", () => {
    expect(phaseFromBlockType("reasoning_start")).toBe("reasoning");
    expect(phaseFromBlockType("reasoning_chunk")).toBe("reasoning");
    expect(phaseFromBlockType("tool_group")).toBe("tool");
    expect(phaseFromBlockType("native_tool")).toBe("tool");
    expect(phaseFromBlockType("text_chunk")).toBe("text");
    expect(phaseFromBlockType("error")).toBe("error");
  });

  it("returns null for blocks with no mascot meaning", () => {
    expect(phaseFromBlockType("response_end")).toBeNull();
    expect(phaseFromBlockType(undefined)).toBeNull();
    expect(phaseFromBlockType("something_else")).toBeNull();
  });
});

describe("deriveMascotState", () => {
  it("rests idle when nothing is happening", () => {
    expect(deriveMascotState(base)).toEqual({ pose: "idle", starPose: null });
  });

  it("thinks while reasoning", () => {
    expect(deriveMascotState({ ...base, streaming: true, phase: "reasoning" })).toEqual({
      pose: "pensando",
      starPose: null,
    });
  });

  it("codes while running a tool", () => {
    expect(deriveMascotState({ ...base, streaming: true, phase: "tool" })).toEqual({
      pose: "codando",
      starPose: null,
    });
  });

  it("faces forward while writing the answer", () => {
    expect(deriveMascotState({ ...base, streaming: true, phase: "text" })).toEqual({
      pose: "frente",
      starPose: null,
    });
  });

  it("thinks when active but the first block hasn't landed", () => {
    expect(deriveMascotState({ ...base, sessionStatus: "running", phase: null })).toEqual({
      pose: "pensando",
      starPose: null,
    });
  });

  it("errors — and the star reacts — on a failed phase, session or response", () => {
    const react = { pose: "erro", starPose: "pensando" };
    expect(deriveMascotState({ ...base, phase: "error" })).toEqual(react);
    expect(deriveMascotState({ ...base, sessionStatus: "failed" })).toEqual(react);
    expect(deriveMascotState({ ...base, activeResponseState: "failed" })).toEqual(react);
  });

  it("error outranks an active stream", () => {
    // A tool that errored mid-run is still an error, not a tool beat.
    expect(
      deriveMascotState({ ...base, streaming: true, phase: "tool", sessionStatus: "failed" }),
    ).toEqual({ pose: "erro", starPose: "pensando" });
  });

  it("celebrates — both happy — only on the transient success beat", () => {
    expect(deriveMascotState({ ...base, justFinished: true })).toEqual({
      pose: "feliz",
      starPose: "feliz",
    });
    // A lingering `completed` state must NOT keep it celebrating.
    expect(deriveMascotState({ ...base, activeResponseState: "completed" })).toEqual({
      pose: "idle",
      starPose: null,
    });
  });

  it("looks lost when offline and otherwise idle", () => {
    expect(deriveMascotState({ ...base, offline: true })).toEqual({
      pose: "pensando",
      starPose: null,
    });
  });
});
