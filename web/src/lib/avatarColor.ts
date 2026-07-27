// Stable avatar tint per identifier — a hash of the id picks from a fixed
// palette so the same agent or connector always wears the same color across
// reloads, and the same one on every screen that draws it.

const AVATAR_COLORS = [
  "#39c9b4",
  "#e8a83c",
  "#a884f0",
  "#4a90e2",
  "#e86a9c",
  "#3ec6e0",
  "#5cbf6a",
  "#f0776a",
];

/** The palette color this id always maps to. */
export function avatarColor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

/** First letters of a name, for the avatar square. Falls back to "?". */
export function avatarInitials(name: string, count = 1): string {
  const letters = (name.match(/[a-z0-9]/gi) ?? []).slice(0, count).join("");
  return letters ? letters.charAt(0).toUpperCase() + letters.slice(1).toLowerCase() : "?";
}
