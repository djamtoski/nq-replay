// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JournalApi } from "./api";
import { createJournal, type JournalDeps } from "./view";
import type { Analytics, CalendarMonth, JTrade, Notes } from "./types";

const trade = (id: number, over: Partial<JTrade> = {}): JTrade => ({
  id,
  session_id: "s1",
  instrument: "NQ",
  point_value: 20,
  side: "long",
  qty: 1,
  entry: 100,
  exit: 105,
  pnl: 100,
  r: 1,
  opened: 1_741_611_600,
  closed: 1_741_611_700,
  reason: "tp",
  mae: 1,
  mfe: 6,
  mae_usd: 20,
  mfe_usd: 120,
  day: "2025-03-10",
  live: true,
  setup: null,
  emotion: null,
  rating: null,
  mistakes: [],
  note: "",
  ...over,
});

const analytics: Analytics = {
  summary: {
    trades: 2,
    wins: 1,
    losses: 1,
    win_rate: 0.5,
    net_pnl: 50,
    profit_factor: 1.5,
    expectancy: 25,
    avg_r: 0.5,
    avg_win: 100,
    avg_loss: -50,
    best: 100,
    worst: -50,
    max_drawdown: 50,
    avg_hold_s: 100,
    avg_mae_usd: 20,
    avg_mfe_usd: 120,
  },
  equity: [
    { t: 1, pnl: 100, cum: 100 },
    { t: 2, pnl: -50, cum: 50 },
  ],
  breakdowns: {
    setup: [
      { key: "ORB", trades: 1, wins: 1, win_rate: 1, pnl: 100, avg_r: 1 },
      { key: "(none)", trades: 1, wins: 0, win_rate: 0, pnl: -50, avg_r: -0.5 },
    ],
    hour: [{ key: 10, trades: 2, wins: 1, win_rate: 0.5, pnl: 50, avg_r: 0.5 }],
    weekday: [{ key: 0, trades: 2, wins: 1, win_rate: 0.5, pnl: 50, avg_r: 0.5 }],
  },
};

const calendar = (over: Partial<CalendarMonth> = {}): CalendarMonth => ({
  year: 2025,
  month: 3,
  days: [
    { day: "2025-03-10", trades: 2, wins: 1, pnl: 50 },
    { day: "2025-03-12", trades: 1, wins: 0, pnl: -80 },
  ],
  net_pnl: -30,
  trades: 3,
  green_days: 1,
  red_days: 1,
  ...over,
});

function setup(
  rows: JTrade[] = [trade(1), trade(2, { closed: 1_741_611_600 })],
  extra: Partial<JournalDeps> = {},
) {
  const db = new Map(rows.map((t) => [t.id, { ...t }]));
  const api = {
    trades: vi.fn(async () => [...db.values()]),
    saveNotes: vi.fn(async (id: number, n: Notes) => {
      const t = db.get(id)!;
      Object.assign(t, n);
      return { ...t };
    }),
    remove: vi.fn(async (id: number) => {
      db.delete(id);
      return { deleted: id };
    }),
    replay: vi.fn(),
    analytics: vi.fn(async () => analytics),
    calendar: vi.fn(async () => calendar()),
    tags: vi.fn(async () => ({
      setups: ["ORB", "Reversal"],
      emotions: ["calm"],
      mistakes: ["FOMO"],
    })),
  };
  const dlg = document.createElement("dialog");
  document.body.append(dlg);
  dlg.showModal = function () {
    this.setAttribute("open", "");
  };
  dlg.close = function () {
    this.removeAttribute("open");
    this.dispatchEvent(new Event("close"));
  };
  const onReplay = vi.fn(async () => null as string | null);
  const j = createJournal(dlg, {
    api: api as unknown as JournalApi,
    fmtTime: (s) => `t${s}`,
    onReplay,
    saveDelayMs: 500,
    ...extra,
  });
  return { api, db, dlg, j, onReplay };
}

const q = <T extends Element = HTMLElement>(root: ParentNode, sel: string) =>
  root.querySelector(sel) as T;
const qa = (root: ParentNode, sel: string) => Array.from(root.querySelectorAll<HTMLElement>(sel));
const input = (el: HTMLInputElement | HTMLTextAreaElement, v: string) => {
  el.value = v;
  el.dispatchEvent(new Event("input", { bubbles: true }));
};
const button = (root: ParentNode, text: string) =>
  qa(root, "button").find((b) => b.textContent === text) as HTMLButtonElement;
const tick = async (ms = 0) => {
  await vi.advanceTimersByTimeAsync(ms);
};

beforeEach(() => {
  vi.useFakeTimers();
  document.body.replaceChildren();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("trades tab", () => {
  it("opens, loads trades and selects the first", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    expect(dlg.open).toBe(true);
    expect(api.trades).toHaveBeenCalledWith({});
    expect(qa(dlg, ".jr-list tbody tr")).toHaveLength(2);
    expect(q(dlg, ".jr-list tbody tr").classList.contains("sel")).toBe(true);
    expect(q(dlg, ".jr-detail h3").textContent).toContain("Long 1 NQ");
    expect(q(dlg, ".jr-list tbody tr").textContent).toContain("t1741611700"); // formatted by deps.fmtTime
  });

  it("shows an empty state", async () => {
    const { j, dlg } = setup([]);
    await j.open();
    expect(q(dlg, ".jr-list .empty").textContent).toContain("No trades yet");
    expect(q(dlg, ".jr-detail").textContent).toContain("Select a trade");
  });

  it("selecting another trade shows its details", async () => {
    const { j, dlg } = setup([trade(1), trade(2, { side: "short", qty: 3 })]);
    await j.open();
    qa(dlg, ".jr-list tbody tr")[1].click();
    await tick();
    expect(q(dlg, ".jr-detail h3").textContent).toContain("Short 3 NQ");
    expect(qa(dlg, ".jr-list tbody tr")[1].classList.contains("sel")).toBe(true);
  });

  it("is keyboard operable", async () => {
    const { j, dlg } = setup([trade(1), trade(2, { qty: 4 })]);
    await j.open();
    qa(dlg, ".jr-list tbody tr")[1].dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
    await tick();
    expect(q(dlg, ".jr-detail h3").textContent).toContain("Long 4 NQ");
  });

  it("renders notes, tags and text as text, never as markup", async () => {
    const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
    const { j, dlg } = setup([
      trade(1, { note: evil, setup: evil, emotion: evil, mistakes: [evil] }),
    ]);
    await j.open();
    expect(dlg.querySelector("img, script")).toBeNull();
    expect(q<HTMLTextAreaElement>(dlg, "textarea").value).toBe(evil);
    expect(q(dlg, ".chip").textContent).toContain(evil);
    expect(q(dlg, ".jr-list tbody tr").textContent).toContain(evil);
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined();
  });

  it("saves typing once, after the debounce, with only the changed field", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const setupIn = q<HTMLInputElement>(dlg, 'input[list="jr-setups"]');
    input(setupIn, "O");
    input(setupIn, "OR");
    input(setupIn, "ORB");
    await tick(499);
    expect(api.saveNotes).not.toHaveBeenCalled();
    expect(q(dlg, ".jr-status").textContent).toBe("Editing…");
    await tick(1);
    expect(api.saveNotes).toHaveBeenCalledTimes(1);
    expect(api.saveNotes).toHaveBeenCalledWith(1, { setup: "ORB" });
    await tick();
    expect(q(dlg, ".jr-status").textContent).toBe("Saved");
    expect(q(dlg, ".jr-list tbody tr").textContent).toContain("ORB"); // the list reflects the saved value
  });

  it("merges edits to different fields into one save", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    input(q(dlg, 'input[list="jr-setups"]'), "ORB");
    input(q(dlg, "textarea"), "plan");
    await tick(500);
    expect(api.saveNotes).toHaveBeenCalledTimes(1);
    expect(api.saveNotes).toHaveBeenCalledWith(1, { setup: "ORB", note: "plan" });
  });

  it("saves the rating immediately and clears it when the same star is clicked again", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const star = (n: number) =>
      q<HTMLButtonElement>(dlg, `.jr-stars button[aria-label^="${n} star"]`);
    star(4).click();
    await tick();
    expect(api.saveNotes).toHaveBeenLastCalledWith(1, { rating: 4 });
    expect(qa(dlg, ".jr-stars button.on")).toHaveLength(4);
    star(4).click();
    await tick();
    expect(api.saveNotes).toHaveBeenLastCalledWith(1, { rating: null });
    expect(qa(dlg, ".jr-stars button.on")).toHaveLength(0);
  });

  it("adds and removes mistake tags, ignoring blanks and duplicates", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const m = q<HTMLInputElement>(dlg, 'input[list="jr-mistakes"]');
    const enter = () =>
      m.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
    m.value = "FOMO";
    enter();
    await tick();
    expect(api.saveNotes).toHaveBeenLastCalledWith(1, { mistakes: ["FOMO"] });
    m.value = "FOMO";
    enter();
    m.value = "   ";
    enter();
    await tick();
    expect(api.saveNotes).toHaveBeenCalledTimes(1);
    m.value = "late";
    enter();
    await tick();
    expect(qa(dlg, ".chip").map((c) => c.textContent)).toEqual(["FOMO×", "late×"]);
    button(dlg, "×").click();
    await tick();
    expect(api.saveNotes).toHaveBeenLastCalledWith(1, { mistakes: ["late"] });
  });

  it("caps mistakes at ten tags", async () => {
    const tags = Array.from({ length: 10 }, (_, i) => `m${i}`);
    const { j, dlg, api } = setup([trade(1, { mistakes: tags })]);
    await j.open();
    const m = q<HTMLInputElement>(dlg, 'input[list="jr-mistakes"]');
    m.value = "eleventh";
    m.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
    );
    await tick();
    expect(api.saveNotes).not.toHaveBeenCalled();
    expect(qa(dlg, ".chip")).toHaveLength(10);
  });

  it("flushes a pending edit before switching to another trade", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    input(q(dlg, "textarea"), "first trade note");
    qa(dlg, ".jr-list tbody tr")[1].click();
    await tick();
    expect(api.saveNotes).toHaveBeenCalledWith(1, { note: "first trade note" }); // saved to trade 1, not 2
    expect(api.saveNotes).toHaveBeenCalledTimes(1);
    expect(q<HTMLTextAreaElement>(dlg, "textarea").value).toBe("");
  });

  it("flushes on blur and when the dialog is closed", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const note = q<HTMLTextAreaElement>(dlg, "textarea");
    input(note, "a");
    note.dispatchEvent(new Event("blur"));
    await tick();
    expect(api.saveNotes).toHaveBeenCalledTimes(1);
    input(note, "ab");
    j.close();
    await tick();
    expect(api.saveNotes).toHaveBeenCalledTimes(2);
    expect(dlg.open).toBe(false);
  });

  it("saves a pending edit when the dialog is closed by Esc, not through our close()", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    input(q(dlg, "textarea"), "typed then Esc");
    dlg.close(); // what the browser does on Esc; fires the dialog's close event
    await tick();
    expect(api.saveNotes).toHaveBeenCalledWith(1, { note: "typed then Esc" });
  });

  it("shows a save failure and keeps the text", async () => {
    const { j, dlg, api } = setup();
    api.saveNotes.mockRejectedValueOnce(new Error("rating must be 1 to 5"));
    await j.open();
    input(q(dlg, "textarea"), "keep me");
    await tick(500);
    expect(q(dlg, ".jr-status").textContent).toBe("Not saved: rating must be 1 to 5");
    expect(q(dlg, ".jr-status").classList.contains("bad")).toBe(true);
    expect(q<HTMLTextAreaElement>(dlg, "textarea").value).toBe("keep me");
  });

  it("deletes only after confirmation", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    button(dlg, "Delete").click();
    await tick();
    expect(api.remove).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    button(dlg, "Delete").click();
    await tick();
    expect(api.remove).toHaveBeenCalledWith(1);
    expect(qa(dlg, ".jr-list tbody tr")).toHaveLength(1);
    expect(q(dlg, ".jr-detail h3").textContent).toContain("Long 1 NQ"); // next trade selected
  });

  it("does not save an edit for a trade that was just deleted", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    input(q(dlg, "textarea"), "doomed");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    button(dlg, "Delete").click();
    await tick(1000);
    expect(api.saveNotes).not.toHaveBeenCalled();
  });

  it("replays a trade and closes the dialog, or shows why it could not", async () => {
    const { j, dlg, onReplay } = setup();
    await j.open();
    button(dlg, "Replay this trade").click();
    await tick();
    expect(onReplay).toHaveBeenCalledWith(1);
    expect(dlg.open).toBe(false);

    onReplay.mockResolvedValueOnce("This trade's session was not saved");
    await j.open();
    button(dlg, "Replay this trade").click();
    await tick();
    expect(dlg.open).toBe(true);
    expect(q(dlg, ".jr-err").textContent).toBe("This trade's session was not saved");
  });

  it("saves pending notes before replaying", async () => {
    const { j, dlg, api, onReplay } = setup();
    await j.open();
    input(q(dlg, "textarea"), "before replay");
    button(dlg, "Replay this trade").click();
    await tick();
    expect(api.saveNotes).toHaveBeenCalledWith(1, { note: "before replay" });
    expect(api.saveNotes.mock.invocationCallOrder[0]).toBeLessThan(
      onReplay.mock.invocationCallOrder[0],
    );
  });

  it("shows an error banner when loading fails", async () => {
    const { j, dlg, api } = setup();
    api.trades.mockRejectedValue(new Error("Can't reach the API"));
    await j.open();
    expect(q(dlg, ".jr-err").textContent).toBe("Can't reach the API");
  });
});

describe("filters", () => {
  it("reloads with the chosen filters and clears them", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const [from, to] = qa(dlg, 'input[type="date"]') as HTMLInputElement[];
    from.value = "2025-03-01";
    from.dispatchEvent(new Event("change"));
    to.value = "2025-03-31";
    to.dispatchEvent(new Event("change"));
    const side = q<HTMLSelectElement>(dlg, 'select[aria-label="Side filter"]');
    side.value = "short";
    side.dispatchEvent(new Event("change"));
    await tick();
    expect(api.trades).toHaveBeenLastCalledWith({
      start: "2025-03-01",
      end: "2025-03-31",
      setup: undefined,
      side: "short",
    });
    button(dlg, "Clear").click();
    await tick();
    expect(api.trades).toHaveBeenLastCalledWith({});
    expect(from.value).toBe("");
    expect(side.value).toBe("");
  });

  it("offers the setups seen so far", async () => {
    const { j, dlg } = setup();
    await j.open();
    const opts = qa(dlg, 'select[aria-label="Setup filter"] option').map((o) => o.textContent);
    expect(opts).toEqual(["All setups", "ORB", "Reversal"]);
    expect(qa(dlg, "datalist#jr-setups option")).toHaveLength(2);
  });

  it("ignores an older response that arrives after a newer one", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    let release!: (v: JTrade[]) => void;
    api.trades.mockImplementationOnce(() => new Promise((r) => (release = r)));
    api.trades.mockImplementationOnce(async () => [trade(9, { qty: 9 })]);
    const [from] = qa(dlg, 'input[type="date"]') as HTMLInputElement[];
    from.value = "2025-03-01";
    from.dispatchEvent(new Event("change")); // slow request
    from.value = "2025-03-02";
    from.dispatchEvent(new Event("change")); // fast request, newer
    await tick();
    release([trade(1, { qty: 1 })]); // the slow one finally answers
    await tick();
    expect(q(dlg, ".jr-detail h3").textContent).toContain("Long 9 NQ");
  });
});

describe("analytics tab", () => {
  it("shows summary cards, an equity curve and breakdowns", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    button(dlg, "Analytics").click();
    await tick();
    expect(api.analytics).toHaveBeenCalledWith({});
    const cards = q(dlg, ".jr-cards").textContent!;
    expect(cards).toContain("+$50.00");
    expect(cards).toContain("50.0%");
    expect(cards).toContain("1m 40s");
    expect(q(dlg, "svg.jr-equity path.curve").getAttribute("d")).toMatch(/^M/);
    const titles = qa(dlg, ".jr-breakdowns h4").map((h) => h.textContent);
    expect(titles).toEqual(["By setup", "By entry hour (NY)", "By weekday"]); // empty breakdowns are skipped
    expect(q(dlg, ".jr-breakdowns").textContent).toContain("10:00");
    expect(q(dlg, ".jr-breakdowns").textContent).toContain("Mon");
  });

  it("explains an empty result", async () => {
    const { j, dlg, api } = setup();
    api.analytics.mockResolvedValue({ summary: { trades: 0 }, equity: [], breakdowns: {} });
    await j.open();
    button(dlg, "Analytics").click();
    await tick();
    expect(q(dlg, ".empty").textContent).toContain("No trades match");
  });

  it("passes the active filters", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    const side = q<HTMLSelectElement>(dlg, 'select[aria-label="Side filter"]');
    side.value = "long";
    side.dispatchEvent(new Event("change"));
    await tick();
    button(dlg, "Analytics").click();
    await tick();
    expect(api.analytics).toHaveBeenLastCalledWith({
      start: undefined,
      end: undefined,
      setup: undefined,
      side: "long",
    });
  });
});

describe("calendar tab", () => {
  it("opens on the month of the newest trade", async () => {
    const { j, dlg, api } = setup([trade(1, { day: "2025-03-10" })]);
    await j.open();
    button(dlg, "Calendar").click();
    await tick();
    expect(api.calendar).toHaveBeenCalledWith(2025, 3);
    expect(q(dlg, ".jr-calhead h3").textContent).toBe("March 2025");
  });

  it("draws days with their totals and a summary", async () => {
    const { j, dlg } = setup();
    await j.open();
    button(dlg, "Calendar").click();
    await tick();
    const d10 = q(dlg, 'td[data-day="2025-03-10"]');
    expect(d10.textContent).toContain("+$50");
    expect(d10.textContent).toContain("2 trades");
    expect(q(dlg, 'td[data-day="2025-03-12"]').textContent).toContain("-$80");
    expect(qa(dlg, "td.has")).toHaveLength(2);
    expect(q(dlg, ".jr-calhead").textContent).toContain("3 trades · 1 green · 1 red days");
    expect(qa(dlg, ".calgrid thead th").map((t) => t.textContent)).toEqual([
      "Mon",
      "Tue",
      "Wed",
      "Thu",
      "Fri",
      "Sat",
      "Sun",
    ]);
  });

  it("navigates months, including across a year", async () => {
    const { j, dlg, api } = setup([trade(1, { day: "2025-01-15" })]);
    await j.open();
    button(dlg, "Calendar").click();
    await tick();
    q<HTMLButtonElement>(dlg, 'button[aria-label="Previous month"]').click();
    await tick();
    expect(api.calendar).toHaveBeenLastCalledWith(2024, 12);
    expect(q(dlg, ".jr-calhead h3").textContent).toBe("December 2024");
    q<HTMLButtonElement>(dlg, 'button[aria-label="Next month"]').click();
    q<HTMLButtonElement>(dlg, 'button[aria-label="Next month"]').click();
    await tick();
    expect(api.calendar).toHaveBeenLastCalledWith(2025, 2);
  });

  it("clicking a day shows that day's trades", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    button(dlg, "Calendar").click();
    await tick();
    q(dlg, 'td[data-day="2025-03-10"]').click();
    await tick();
    expect(api.trades).toHaveBeenLastCalledWith({ start: "2025-03-10", end: "2025-03-10" });
    expect(q(dlg, ".jr-trades")).not.toBeNull();
    expect(q<HTMLInputElement>(dlg, 'input[aria-label="From day"]').value).toBe("2025-03-10");
    expect(q(dlg, '[data-tab="trades"]').classList.contains("active")).toBe(true);
  });

  it("days without trades are not clickable", async () => {
    const { j, dlg, api } = setup();
    await j.open();
    button(dlg, "Calendar").click();
    await tick();
    const before = api.trades.mock.calls.length;
    q(dlg, 'td[data-day="2025-03-11"]').click();
    await tick();
    expect(api.trades.mock.calls.length).toBe(before);
  });
});
