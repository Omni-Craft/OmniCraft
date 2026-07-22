// Tests for the boot-splash dismissal rule (src/splash.js), run with
// `node --test` (no extra deps).
//
// The rule guards one thing: the splash must not be pulled while the screen is
// still black. That means dismissing on the server page (the reveal), and on
// the setup page ONLY when nothing is booting behind it — never on the
// transient setup-page fallback that a cold boot passes through on its way up.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { shouldDismissSplash } = require("../src/splash");

const SERVER = "http://127.0.0.1:6767";
const SETUP = "file:///app/setup/index.html";

describe("dismiss when a real page is showing", () => {
  it("dismisses once the server page has loaded", () => {
    assert.equal(
      shouldDismissSplash({
        loadedUrl: "http://127.0.0.1:6767/chat",
        pinnedOrigin: SERVER,
        autostartInProgress: false,
        setupPageUrl: SETUP,
      }),
      true,
    );
  });

  it("dismisses on the setup page when nothing is booting", () => {
    // Fresh or remote user: the setup page IS the destination.
    assert.equal(
      shouldDismissSplash({
        loadedUrl: SETUP,
        pinnedOrigin: null,
        autostartInProgress: false,
        setupPageUrl: SETUP,
      }),
      true,
    );
  });
});

describe("hold the splash while the screen would still be black", () => {
  it("does NOT dismiss on the setup-page fallback during a cold boot", () => {
    // The window fell back to setup because the server was refusing connections
    // while it boots. Dismissing here is the exact bug — black returns the
    // moment the server page replaces setup.
    assert.equal(
      shouldDismissSplash({
        loadedUrl: SETUP,
        pinnedOrigin: SERVER,
        autostartInProgress: true,
        setupPageUrl: SETUP,
      }),
      false,
    );
  });

  it("does not dismiss on an unrelated origin", () => {
    assert.equal(
      shouldDismissSplash({
        loadedUrl: "about:blank",
        pinnedOrigin: SERVER,
        autostartInProgress: true,
        setupPageUrl: SETUP,
      }),
      false,
    );
  });

  it("still reveals the server page even while autostart reports in progress", () => {
    // The server page loading is the definitive reveal — it outranks the flag.
    assert.equal(
      shouldDismissSplash({
        loadedUrl: "http://127.0.0.1:6767/",
        pinnedOrigin: SERVER,
        autostartInProgress: true,
        setupPageUrl: SETUP,
      }),
      true,
    );
  });
});
