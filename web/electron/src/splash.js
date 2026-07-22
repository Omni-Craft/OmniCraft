// When to take down the boot splash, kept pure so the timing rule can be tested
// without a BrowserWindow.
//
// The splash exists to cover one specific gap: on a cold launch the local
// server is still coming up, and the main window shows black until its page
// loads. The rule below decides, per page load, whether the window is finally
// showing something real and the splash can go.
//
// The subtlety is the cold-boot fallback. The main window first tries the saved
// server, fails with connection-refused while the server boots, and lands on
// the setup page — a real page load that is NOT the moment to reveal, because
// the server is about to replace it. So the setup page dismisses the splash
// only when NO auto-start is in progress (a fresh or remote user, where setup
// IS the destination).

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
 * Whether a finished page load means the splash can be dismissed.
 *
 * @param {object} params
 * @param {string} params.loadedUrl The URL the main window just finished.
 * @param {string | null} params.pinnedOrigin Origin the window is pinned to.
 * @param {boolean} params.autostartInProgress Whether the local server is
 *   still being booted and re-pointed to.
 * @param {string | null} params.setupPageUrl The setup page's file:// URL.
 * @returns {boolean}
 */
function shouldDismissSplash({ loadedUrl, pinnedOrigin, autostartInProgress, setupPageUrl }) {
  // The server page is up: this is the "it works" moment the splash was hiding.
  if (pinnedOrigin && originOf(loadedUrl) === pinnedOrigin) return true;
  // The setup page is the real destination only when nothing is booting behind
  // it; during a cold boot it is a transient fallback, not the reveal.
  if (!autostartInProgress && setupPageUrl && loadedUrl === setupPageUrl) return true;
  return false;
}

module.exports = { shouldDismissSplash, originOf };
