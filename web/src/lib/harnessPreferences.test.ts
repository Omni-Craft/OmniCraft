import { afterEach, describe, expect, it, vi } from "vitest";
import { readLastHarness, writeLastHarness } from "./harnessPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("harnessPreferences", () => {
  it("returns null when nothing is stored", () => {
    expect(readLastHarness("ag_fucho")).toBeNull();
  });

  it("returns null for null/undefined agent id", () => {
    expect(readLastHarness(null)).toBeNull();
    expect(readLastHarness(undefined)).toBeNull();
  });

  it("round-trips a written harness override", () => {
    writeLastHarness("ag_fucho", "openai-agents");
    expect(readLastHarness("ag_fucho")).toBe("openai-agents");
  });

  it("stores per-agent preferences independently", () => {
    writeLastHarness("ag_fucho", "openai-agents");
    writeLastHarness("ag_lilo", "claude-sdk");
    expect(readLastHarness("ag_fucho")).toBe("openai-agents");
    expect(readLastHarness("ag_lilo")).toBe("claude-sdk");
  });

  it("overwrites the previous pick for the same agent", () => {
    writeLastHarness("ag_fucho", "openai-agents");
    writeLastHarness("ag_fucho", "claude-sdk");
    expect(readLastHarness("ag_fucho")).toBe("claude-sdk");
  });

  it("clears the override when null is written", () => {
    writeLastHarness("ag_fucho", "openai-agents");
    writeLastHarness("ag_fucho", null);
    expect(readLastHarness("ag_fucho")).toBeNull();
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastHarness("ag_x", "claude-sdk")).not.toThrow();
    expect(readLastHarness("ag_x")).toBeNull();
  });
});
