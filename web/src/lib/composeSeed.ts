// A one-shot handoff for pre-filling the home composer's prompt from elsewhere
// in the app (e.g. "start a session from this GitHub issue"). The source page
// stashes the text, navigates to "/", and the composer consumes it once on
// mount — kept in sessionStorage so a long issue body never bloats the URL.

const KEY = "omnicraft.compose.seed";

/** Stash a prompt for the next home-composer mount to pick up. */
export function setComposeSeed(text: string): void {
  try {
    sessionStorage.setItem(KEY, text);
  } catch {
    /* private mode / storage disabled — seeding is best-effort */
  }
}

/** Read and clear the stashed prompt (returns null when none is pending). */
export function takeComposeSeed(): string | null {
  try {
    const value = sessionStorage.getItem(KEY);
    if (value !== null) sessionStorage.removeItem(KEY);
    return value;
  } catch {
    return null;
  }
}
