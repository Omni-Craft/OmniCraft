import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pushBlocker } from "./webPush";

vi.mock("@/lib/nativeBridge", () => ({ isNativeShell: () => false }));

const originalUA = navigator.userAgent;

function setUserAgent(value: string) {
  Object.defineProperty(navigator, "userAgent", { value, configurable: true });
}

// jsdom ships no service worker, which would short-circuit every case before
// the check under test.
beforeEach(() => {
  Object.defineProperty(navigator, "serviceWorker", { value: {}, configurable: true });
});

afterEach(() => {
  setUserAgent(originalUA);
  Reflect.deleteProperty(navigator, "serviceWorker");
  vi.unstubAllGlobals();
});

describe("pushBlocker", () => {
  it("names the Home Screen requirement on an iPhone", () => {
    // Safari only exposes PushManager to an installed PWA, so a phone in a
    // normal tab looks exactly like an unsupported browser — the message has
    // to say what to do about it.
    setUserAgent("Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15");
    const reason = pushBlocker();
    expect(reason).toMatch(/Tela de Início/);
  });

  it("gives a plain answer on a desktop browser without push", () => {
    setUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
    expect(pushBlocker()).toBe("Este navegador não suporta Web Push.");
  });

  it("clears once push is available in a secure context", () => {
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("isSecureContext", true);
    expect(pushBlocker()).toBeNull();
  });

  it("flags plain http, where the browser refuses push", () => {
    vi.stubGlobal("PushManager", class {});
    vi.stubGlobal("isSecureContext", false);
    expect(pushBlocker()).toMatch(/https/);
  });
});
