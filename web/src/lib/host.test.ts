import { afterEach, describe, expect, it } from "vitest";

import { getCliServerUrl, setOmniCraftHostConfig } from "./host";

afterEach(() => {
  setOmniCraftHostConfig({});
});

describe("getCliServerUrl", () => {
  it("returns window.location.origin when no suffix is configured", () => {
    setOmniCraftHostConfig({});
    const url = getCliServerUrl();
    expect(url).toBe(window.location.origin);
  });

  it("appends the configured cliServerUrlSuffix", () => {
    setOmniCraftHostConfig({ cliServerUrlSuffix: "/api/2.0/omnicraft" });
    const url = getCliServerUrl();
    expect(url).toBe(`${window.location.origin}/api/2.0/omnicraft`);
  });

  it("handles an empty string suffix the same as no suffix", () => {
    setOmniCraftHostConfig({ cliServerUrlSuffix: "" });
    expect(getCliServerUrl()).toBe(window.location.origin);
  });
});
