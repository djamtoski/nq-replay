import { describe, expect, it, vi } from "vitest";
import { createApi, query } from "./api";

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200 });

describe("query", () => {
  it("skips empty values and encodes the rest", () => {
    expect(query({})).toBe("");
    expect(query({ a: undefined, b: null, c: "" })).toBe("");
    expect(query({ setup: "a b&c", limit: 5 })).toBe("?setup=a+b%26c&limit=5");
  });
});

describe("journal api", () => {
  it("builds list, analytics and calendar urls from filters", async () => {
    const f = vi.fn().mockImplementation(async () => ok({ trades: [] }));
    const api = createApi("http://x", f as unknown as typeof fetch);
    await api.trades({ start: "2025-03-01", side: "long" }, 50);
    expect(f.mock.calls[0][0]).toBe("http://x/journal/trades?start=2025-03-01&side=long&limit=50");
    await api.analytics({ setup: "ORB" });
    expect(f.mock.calls[1][0]).toBe("http://x/journal/analytics?setup=ORB");
    await api.calendar(2025, 3);
    expect(f.mock.calls[2][0]).toBe("http://x/journal/calendar?year=2025&month=3");
    await api.replay(7);
    expect(f.mock.calls[3][0]).toBe("http://x/journal/trades/7/replay");
  });

  it("sends notes as a JSON PATCH and deletes with DELETE", async () => {
    const f = vi.fn().mockImplementation(async () => ok({ id: 3 }));
    const api = createApi("http://x", f as unknown as typeof fetch);
    await api.saveNotes(3, { rating: 4, note: "n" });
    const [url, init] = f.mock.calls[0];
    expect(url).toBe("http://x/journal/trades/3");
    expect(init.method).toBe("PATCH");
    expect(init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(init.body)).toEqual({ rating: 4, note: "n" });
    await api.remove(3);
    expect(f.mock.calls[1][1].method).toBe("DELETE");
  });

  it("surfaces the server's message, a status text, or a connection hint", async () => {
    const api = (impl: () => Promise<Response>) => createApi("http://x", impl as typeof fetch);
    await expect(
      api(
        async () =>
          new Response(JSON.stringify({ detail: "rating must be 1 to 5" }), { status: 422 }),
      ).tags(),
    ).rejects.toThrow("rating must be 1 to 5");
    await expect(
      api(async () => new Response("oops", { status: 500, statusText: "Server Error" })).tags(),
    ).rejects.toThrow("Request failed");
    await expect(
      api(
        async () =>
          new Response(JSON.stringify({ detail: [{ msg: "x" }] }), {
            status: 422,
            statusText: "Bad",
          }),
      ).tags(),
    ).rejects.toThrow("Request failed");
    await expect(
      api(async () => {
        throw new TypeError("Failed to fetch");
      }).tags(),
    ).rejects.toThrow("Can't reach the server. Is it running?");
  });
});
