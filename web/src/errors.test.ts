import { describe, expect, it } from "vitest";
import { errorMessage } from "./errors";

describe("errorMessage", () => {
  it("passes short sentences through", () => {
    expect(errorMessage(new Error("No data for 2026-09-01 to 2026-10-01"))).toBe(
      "No data for 2026-09-01 to 2026-10-01",
    );
    expect(errorMessage("Session not found")).toBe("Session not found");
  });

  it("turns network failures into one friendly line", () => {
    const friendly = "Can't reach the server. Is it running?";
    for (const m of [
      "Failed to fetch",
      "NetworkError when attempting to fetch resource.",
      "Load failed",
      "",
    ]) {
      expect(errorMessage(new Error(m))).toBe(friendly);
    }
    expect(errorMessage(undefined)).toBe(friendly);
  });

  it("never shows file paths", () => {
    const win = String.raw`no files under C:\Users\me\data`;
    expect(errorMessage(new Error(win))).toBe("Something went wrong");
    expect(errorMessage(new Error("cannot open /home/me/data/x"))).toBe("Something went wrong");
  });

  it("collapses whitespace and caps the length", () => {
    expect(errorMessage(new Error("a\n  b"))).toBe("a b");
    const long = errorMessage(new Error("x".repeat(500)));
    expect(long).toHaveLength(140);
    expect(long.endsWith("…")).toBe(true);
  });
});
