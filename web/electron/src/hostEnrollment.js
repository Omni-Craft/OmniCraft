// The decision behind the "Allow this machine as a runner?" dialog, kept pure
// so it can be tested without Electron's dialog or BrowserWindow.
//
// Enrolling this machine executes agent code the server dispatches, so the
// grant is guarded two ways: a PERSISTENT "Always Allow" is offered only when
// the request is trustworthy, and an existing grant is honored only under the
// same condition. What counts as trustworthy differs by caller:
//
//   - A page-initiated request (the server's own SPA calling host-control) is
//     trusted only while the window's visible top-level page IS its pinned
//     origin. A foreign page reached via redirect can be allowed once, never
//     remembered — otherwise a compromised server could earn a standing grant.
//
//   - A main-process auto-start of a loopback server the user saved carries a
//     `trustedOrigin`. That path is not page-controllable and the origin is the
//     user's own saved server, so it may always be remembered — and must be,
//     because a cold server boot can leave the page unloaded when the prompt
//     fires, and a timing race must not cost the user the remember option (the
//     bug this fixes: "Allow Once" every launch, forever).

"use strict";

/**
 * Origin of a URL, or null when it does not parse.
 *
 * @param {string | undefined | null} url
 * @returns {string | null}
 */
function originOf(url) {
  try {
    return new URL(url ?? "").origin;
  } catch {
    return null;
  }
}

/**
 * Decide how to prompt for host enrollment.
 *
 * @param {object} params
 * @param {string | null} params.pinnedOrigin Origin the window is pinned to.
 * @param {string | undefined | null} params.currentUrl The window's visible
 *   top-level URL.
 * @param {string | null} [params.trustedOrigin] A main-process-supplied origin
 *   trusted without page proof (loopback auto-start).
 * @param {unknown} params.approvedOrigins Persisted `allowed_hosting_origins`.
 * @returns {{origin: string | null, canRemember: boolean, alreadyApproved: boolean}}
 *   `origin` to enroll (null → nothing to do); `canRemember` → offer/honor a
 *   persistent grant; `alreadyApproved` → skip the dialog entirely.
 */
function hostEnrollmentDecision({
  pinnedOrigin,
  currentUrl,
  trustedOrigin = null,
  approvedOrigins,
}) {
  const origin = trustedOrigin ?? pinnedOrigin ?? null;
  const onPinnedPage = origin != null && originOf(currentUrl) === pinnedOrigin;
  const canRemember = trustedOrigin != null || onPinnedPage;
  const list = Array.isArray(approvedOrigins) ? approvedOrigins : [];
  const alreadyApproved = canRemember && origin != null && list.includes(origin);
  return { origin, canRemember, alreadyApproved };
}

module.exports = { hostEnrollmentDecision, originOf };
