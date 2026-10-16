import type { Analytics, CalendarMonth, Filters, JTrade, Notes, ReplayInfo, Tags } from "./types";

export function query(params: Record<string, string | number | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Thin typed client for the journal endpoints. `f` is injectable for tests. */
export function createApi(base: string, f: typeof fetch = (...a) => fetch(...a)) {
  async function req<T>(path: string, init?: RequestInit): Promise<T> {
    let r: Response;
    try {
      r = await f(base + path, init);
    } catch {
      throw new Error(`Can't reach the API at ${base}. Is the server running?`);
    }
    if (!r.ok) {
      const j = (await r.json().catch(() => ({}))) as { detail?: unknown };
      throw new Error(typeof j.detail === "string" ? j.detail : r.statusText);
    }
    return (await r.json()) as T;
  }
  const json = (method: string, body: unknown): RequestInit => ({
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return {
    trades: async (flt: Filters = {}, limit = 500): Promise<JTrade[]> =>
      (await req<{ trades: JTrade[] }>(`/journal/trades${query({ ...flt, limit })}`)).trades,
    saveNotes: (id: number, notes: Notes) =>
      req<JTrade>(`/journal/trades/${id}`, json("PATCH", notes)),
    remove: (id: number) => req<{ deleted: number }>(`/journal/trades/${id}`, { method: "DELETE" }),
    replay: (id: number) => req<ReplayInfo>(`/journal/trades/${id}/replay`),
    analytics: (flt: Filters = {}) => req<Analytics>(`/journal/analytics${query({ ...flt })}`),
    calendar: (year: number, month: number) =>
      req<CalendarMonth>(`/journal/calendar${query({ year, month })}`),
    tags: () => req<Tags>("/journal/tags"),
  };
}

export type JournalApi = ReturnType<typeof createApi>;
