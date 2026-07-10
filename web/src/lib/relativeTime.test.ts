import { describe, expect, it } from "vitest";
import { relativeTime } from "./relativeTime";

const NOW = new Date("2026-05-28T12:00:00Z").getTime();
const MIN = 60_000;
const HR = 60 * MIN;
const DAY = 24 * HR;
const WEEK = 7 * DAY;
const MONTH = 30 * DAY;
const YEAR = 365 * DAY;

describe("relativeTime", () => {
  it("returns 'agora' for diffs under one minute", () => {
    expect(relativeTime(NOW - 30_000, NOW)).toBe("agora");
    expect(relativeTime(NOW - 59_000, NOW)).toBe("agora");
  });

  it("uses 'Nmin' for minutes (floor)", () => {
    expect(relativeTime(NOW - MIN, NOW)).toBe("1min");
    expect(relativeTime(NOW - 5 * MIN, NOW)).toBe("5min");
    expect(relativeTime(NOW - 59 * MIN, NOW)).toBe("59min");
  });

  it("uses 'Nh' for hours", () => {
    expect(relativeTime(NOW - HR, NOW)).toBe("1h");
    expect(relativeTime(NOW - 23 * HR, NOW)).toBe("23h");
  });

  it("uses 'Nd' for days", () => {
    expect(relativeTime(NOW - DAY, NOW)).toBe("1d");
    expect(relativeTime(NOW - 6 * DAY, NOW)).toBe("6d");
  });

  it("uses 'Nsem' for weeks", () => {
    expect(relativeTime(NOW - WEEK, NOW)).toBe("1sem");
    expect(relativeTime(NOW - 3 * WEEK, NOW)).toBe("3sem");
  });

  it("uses 'Nmes' (not 'Nmin') for months to disambiguate from minutes", () => {
    // The whole point of the 'mes' suffix: 1mes and 1min must not collide.
    expect(relativeTime(NOW - MONTH, NOW)).toBe("1mes");
    expect(relativeTime(NOW - 6 * MONTH, NOW)).toBe("6mes");
    expect(relativeTime(NOW - MONTH, NOW)).not.toBe("1min");
  });

  it("uses 'Na' for years", () => {
    expect(relativeTime(NOW - YEAR, NOW)).toBe("1a");
    expect(relativeTime(NOW - 3 * YEAR, NOW)).toBe("3a");
  });

  it("clamps future timestamps to 'agora' (no negative diffs)", () => {
    // Clock skew can briefly put the server ahead of the client;
    // surface no time rather than a negative.
    expect(relativeTime(NOW + 5_000, NOW)).toBe("agora");
  });

  it("rolls cleanly at unit boundaries", () => {
    // Just under a unit boundary keeps the smaller unit; at the
    // boundary, the next unit starts. Pins the floor semantics.
    expect(relativeTime(NOW - (HR - 1), NOW)).toBe("59min");
    expect(relativeTime(NOW - HR, NOW)).toBe("1h");
    expect(relativeTime(NOW - (DAY - 1), NOW)).toBe("23h");
    expect(relativeTime(NOW - DAY, NOW)).toBe("1d");
    expect(relativeTime(NOW - (WEEK - 1), NOW)).toBe("6d");
    expect(relativeTime(NOW - WEEK, NOW)).toBe("1sem");
  });
});
