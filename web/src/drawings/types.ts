export type DrawType = "trend" | "ray" | "arrow" | "hline" | "hray" | "vline" | "rect" | "ellipse"
  | "channel" | "fib" | "long" | "short" | "measure" | "text" | "brush";
export type ToolId = "cursor" | DrawType;

/** A point in DATA space: epoch seconds + price. Independent of timeframe and zoom. */
export interface Pt { t: number; p: number }
export interface Style { color: string; width: number; dash: boolean }

export interface Drawing {
  id: string;
  type: DrawType;
  pts: Pt[];
  style: Style;
  text?: string;       // text tool
  stop?: number;       // long/short: stop price (target is pts[1].p)
}

/** Clicks needed to finish a drawing (brush is freehand: 0). */
export const NEED: Record<DrawType, number> = {
  trend: 2, ray: 2, arrow: 2, hline: 1, hray: 1, vline: 1, rect: 2, ellipse: 2,
  channel: 3, fib: 2, long: 2, short: 2, measure: 2, text: 1, brush: 0,
};

export const svg = (inner: string) =>
  `<svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;

export const TOOLS: { id: ToolId; label: string; icon: string }[] = [
  { id: "cursor", label: "Select / move (Esc)", icon: svg('<path d="M5 3l10 6-4.5 1.2L8.5 15z" fill="currentColor"/>') },
  { id: "trend", label: "Trend line", icon: svg('<path d="M3 16L17 4"/><circle cx="3" cy="16" r="1.6" fill="currentColor"/><circle cx="17" cy="4" r="1.6" fill="currentColor"/>') },
  { id: "ray", label: "Ray", icon: svg('<path d="M3 16L18 5"/><path d="M14.5 4.5L18 5l-.8 3.4"/><circle cx="3" cy="16" r="1.6" fill="currentColor"/>') },
  { id: "arrow", label: "Arrow", icon: svg('<path d="M3 17L16 4"/><path d="M9 4h7v7"/>') },
  { id: "hline", label: "Horizontal line", icon: svg('<path d="M2 10h16"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/>') },
  { id: "hray", label: "Horizontal ray", icon: svg('<path d="M4 10h14"/><circle cx="4" cy="10" r="1.6" fill="currentColor"/>') },
  { id: "vline", label: "Vertical line", icon: svg('<path d="M10 2v16"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/>') },
  { id: "rect", label: "Rectangle", icon: svg('<rect x="3.5" y="5.5" width="13" height="9" rx="1"/>') },
  { id: "ellipse", label: "Ellipse", icon: svg('<ellipse cx="10" cy="10" rx="7" ry="5"/>') },
  { id: "channel", label: "Parallel channel", icon: svg('<path d="M3 14L16 6M3 18L16 10"/>') },
  { id: "fib", label: "Fib retracement", icon: svg('<path d="M3 4h14M3 8h14M3 12h14M3 16h14" stroke-dasharray="2 2"/>') },
  { id: "long", label: "Long position", icon: svg('<rect x="3" y="3" width="14" height="7" fill="currentColor" fill-opacity=".25"/><rect x="3" y="10" width="14" height="7"/>') },
  { id: "short", label: "Short position", icon: svg('<rect x="3" y="10" width="14" height="7" fill="currentColor" fill-opacity=".25"/><rect x="3" y="3" width="14" height="7"/>') },
  { id: "measure", label: "Measure", icon: svg('<path d="M3 15L15 3l2 2L5 17z"/><path d="M7 11l1.5 1.5M10 8l1.5 1.5"/>') },
  { id: "text", label: "Text", icon: svg('<path d="M5 5h10M10 5v11M8 16h4"/>') },
  { id: "brush", label: "Brush", icon: svg('<path d="M3 16c3-8 5 2 7-4s4-6 7-8"/>') },
];

export const COLORS = ["#2962ff", "#26a69a", "#ef5350", "#f0b429", "#ab47bc", "#d1d4dc"];
export const DEFAULT_STYLE: Style = { color: "#2962ff", width: 2, dash: false };
