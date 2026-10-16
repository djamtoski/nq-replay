// One place that turns anything thrown into the short sentence shown to the user.
// The server already sends plain sentences (no paths, no internals); this covers the rest.

const MAX = 140;

export function errorMessage(e: unknown): string {
  const raw = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  const text = raw.trim();
  if (!text || /^(failed to fetch|networkerror|load failed)/i.test(text)) {
    return "Can't reach the server. Is it running?";
  }
  if (/[a-z]:\\|\/(?:home|users|usr|var|tmp)\//i.test(text)) return "Something went wrong"; // never show file paths
  const one = text.replace(/\s+/g, " ");
  return one.length > MAX ? `${one.slice(0, MAX - 1)}…` : one;
}
