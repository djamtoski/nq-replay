// Pure rules for the New session dialog (no DOM), so they can be tested.

export interface Dataset {
  symbol: string;
  label: string;
  first: string; // YYYY-MM-DD
  last: string;
  days: number;
}

export const SYNTH_DEFAULT = { start: "2025-03-03", end: "2025-03-07" };
export const SYNTH_MAX_DAYS = 10;

export interface Range {
  start: string;
  end: string;
}

/** Keep a chosen range inside the data that exists; an empty or inverted choice becomes the whole dataset. */
export function clampToDataset(start: string, end: string, d: Dataset): Range {
  // dates from somewhere else (the synthetic defaults, another dataset) that miss this data entirely: show all of it
  if (start && end && (end < d.first || start > d.last)) return { start: d.first, end: d.last };
  const inside = (x: string) => (x < d.first ? d.first : x > d.last ? d.last : x);
  const s = start ? inside(start) : d.first;
  const e = end ? inside(end) : d.last;
  return e < s ? { start: d.first, end: d.last } : { start: s, end: e };
}

export function synthHint(): string {
  return `Generated 1-second bars, up to ${SYNTH_MAX_DAYS} trading days. The replay starts on the first day at the time above; the 3 trading days before it are chart history.`;
}

export function realHint(d: Dataset | undefined, count: number): string {
  if (!count || !d) {
    return "No real data downloaded yet. The README explains how to download it (free Nasdaq 100 data or Databento).";
  }
  return `Downloaded: ${d.first} to ${d.last} (${d.days} days). The replay starts on the start date at the time above; the 3 trading days before it are chart history.`;
}

/** Trading days (Mon-Fri) in [start, end], for the synthetic limit. */
export function weekdaysBetween(start: string, end: string): number {
  const a = Date.parse(`${start}T00:00:00Z`);
  const b = Date.parse(`${end}T00:00:00Z`);
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return 0;
  let n = 0;
  for (let t = a; t <= b; t += 86_400_000) {
    const wd = new Date(t).getUTCDay();
    if (wd !== 0 && wd !== 6) n++;
  }
  return n;
}

/** Message for a bad choice, or null when the dialog may submit. */
export function validate(
  source: string,
  start: string,
  end: string,
  d: Dataset | undefined,
): string | null {
  if (!start || !end) return "Pick a start and end date";
  if (end < start) return "The end date is before the start date";
  if (source === "parquet") {
    if (!d) return "No real data downloaded yet";
    if (end < d.first || start > d.last) return `No data for ${start} to ${end}`;
  } else if (weekdaysBetween(start, end) > SYNTH_MAX_DAYS) {
    return `Synthetic data is limited to ${SYNTH_MAX_DAYS} trading days`;
  }
  return null;
}
