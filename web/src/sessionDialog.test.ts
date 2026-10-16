import { describe, expect, it } from "vitest";
import {
  clampToDataset,
  realHint,
  synthHint,
  validate,
  weekdaysBetween,
  type Dataset,
} from "./sessionDialog";

const ds: Dataset = {
  symbol: "USATECH.IDX",
  label: "Nasdaq 100 CFD (Dukascopy)",
  first: "2024-01-02",
  last: "2024-12-31",
  days: 252,
};

describe("clampToDataset", () => {
  it("keeps a range that is inside the data", () => {
    expect(clampToDataset("2024-03-01", "2024-03-29", ds)).toEqual({
      start: "2024-03-01",
      end: "2024-03-29",
    });
  });
  it("pulls the edges in to the downloaded days", () => {
    expect(clampToDataset("2023-06-01", "2025-06-01", ds)).toEqual({
      start: ds.first,
      end: ds.last,
    });
    expect(clampToDataset("2023-06-01", "2024-02-01", ds)).toEqual({
      start: ds.first,
      end: "2024-02-01",
    });
  });
  it("fills in blanks with the whole dataset and fixes an inverted range", () => {
    expect(clampToDataset("", "", ds)).toEqual({ start: ds.first, end: ds.last });
    expect(clampToDataset("2024-05-01", "", ds)).toEqual({ start: "2024-05-01", end: ds.last });
    expect(clampToDataset("2024-06-01", "2024-05-01", ds)).toEqual({
      start: ds.first,
      end: ds.last,
    });
  });
});

describe("weekdaysBetween", () => {
  it("counts Mon-Fri only, inclusive", () => {
    expect(weekdaysBetween("2025-03-03", "2025-03-07")).toBe(5);
    expect(weekdaysBetween("2025-03-03", "2025-03-14")).toBe(10);
    expect(weekdaysBetween("2025-03-08", "2025-03-09")).toBe(0); // a weekend
    expect(weekdaysBetween("2025-03-10", "2025-03-10")).toBe(1);
  });
  it("is 0 for bad or inverted input", () => {
    expect(weekdaysBetween("", "2025-03-10")).toBe(0);
    expect(weekdaysBetween("2025-03-10", "2025-03-03")).toBe(0);
  });
});

describe("validate", () => {
  it("requires both dates and a sane order", () => {
    expect(validate("synthetic", "", "2025-03-07", undefined)).toBe("Pick a start and end date");
    expect(validate("synthetic", "2025-03-07", "2025-03-03", undefined)).toBe(
      "The end date is before the start date",
    );
  });
  it("limits synthetic ranges to 10 trading days", () => {
    expect(validate("synthetic", "2025-03-03", "2025-03-14", undefined)).toBeNull();
    expect(validate("synthetic", "2025-03-03", "2025-03-17", undefined)).toBe(
      "Synthetic data is limited to 10 trading days",
    );
  });
  it("needs a dataset that overlaps the dates for real data", () => {
    expect(validate("parquet", "2024-03-01", "2024-03-05", undefined)).toBe(
      "No real data downloaded yet",
    );
    expect(validate("parquet", "2024-03-01", "2024-03-05", ds)).toBeNull();
    expect(validate("parquet", "2026-09-01", "2026-10-01", ds)).toBe(
      "No data for 2026-09-01 to 2026-10-01",
    );
    expect(validate("parquet", "2023-01-02", "2023-01-06", ds)).toBe(
      "No data for 2023-01-02 to 2023-01-06",
    );
    expect(validate("parquet", "2023-12-01", "2024-01-03", ds)).toBeNull(); // overlaps the first days
  });
});

describe("hints", () => {
  it("describe where the replay starts", () => {
    expect(synthHint()).toContain("starts on the first day");
    expect(realHint(ds, 1)).toContain("2024-01-02 to 2024-12-31 (252 days)");
    expect(realHint(ds, 1)).toContain("starts on the start date");
  });
  it("say so when nothing is downloaded", () => {
    expect(realHint(undefined, 0)).toContain("No real data downloaded yet");
  });
});
