// Tests for the host-enrollment decision (src/hostEnrollment.js), run with
// `node --test` (no extra deps).
//
// Two properties matter here and they pull in opposite directions. A persistent
// grant must never be handed to a page that only reached a pinned window via
// redirect (a compromised server must not earn a standing right to run code).
// But the main-process auto-start of the user's own saved loopback server must
// ALWAYS be able to remember — a cold-boot timing race left users with an
// un-rememberable "Allow Once" on every launch, which is the bug this fixes.

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { hostEnrollmentDecision } = require("../src/hostEnrollment");

const PINNED = "http://127.0.0.1:6767";

describe("a page-initiated request", () => {
  it("may be remembered while the page is on its pinned origin", () => {
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "http://127.0.0.1:6767/chat",
      approvedOrigins: [],
    });

    assert.equal(d.origin, PINNED);
    assert.equal(d.canRemember, true);
  });

  it("may NOT be remembered from a foreign page (redirect)", () => {
    // The security gate: a page that is not on the pinned origin can be allowed
    // once, never remembered — else a compromised server earns a standing grant.
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "http://evil.example.com/",
      approvedOrigins: [],
    });

    assert.equal(d.canRemember, false);
  });

  it("does not skip the dialog for an approved origin it cannot vouch for", () => {
    // Approved in settings, but the visible page is not the pinned origin — the
    // grant must not be silently honored from a page we can't trust.
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "http://evil.example.com/",
      approvedOrigins: [PINNED],
    });

    assert.equal(d.alreadyApproved, false);
  });
});

describe("a trusted (main-process loopback) request", () => {
  it("may be remembered even with the page still unloaded on a cold boot", () => {
    // The fix: url is empty because the server is still coming up. Without the
    // trusted origin this yields "Allow Once" forever.
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "",
      trustedOrigin: PINNED,
      approvedOrigins: [],
    });

    assert.equal(d.origin, PINNED);
    assert.equal(d.canRemember, true);
    assert.equal(d.alreadyApproved, false);
  });

  it("skips the dialog once the origin has been approved", () => {
    // The steady state after the user clicks "Always Allow" once: no prompt,
    // even on a cold boot where the page has not loaded.
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "",
      trustedOrigin: PINNED,
      approvedOrigins: [PINNED],
    });

    assert.equal(d.alreadyApproved, true);
  });
});

describe("nothing to enroll", () => {
  it("returns a null origin when neither pinned nor trusted", () => {
    const d = hostEnrollmentDecision({
      pinnedOrigin: null,
      currentUrl: "",
      approvedOrigins: [],
    });

    assert.equal(d.origin, null);
    assert.equal(d.canRemember, false);
  });

  it("tolerates a malformed approvedOrigins value", () => {
    const d = hostEnrollmentDecision({
      pinnedOrigin: PINNED,
      currentUrl: "http://127.0.0.1:6767/",
      approvedOrigins: "not-an-array",
    });

    assert.equal(d.alreadyApproved, false);
  });
});
