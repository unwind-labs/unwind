import { create } from "zustand";

export type PaneKey = "sessions" | "thread";

interface UiState {
  slug: string | null;
  rootSessionId: string | null;
  threadSessionId: string | null;
  includeMeta: boolean;
  sessionFilter: string;
  showForks: boolean;
  callsOnly: boolean;
  focusedPane: PaneKey;
  /** When set, the right pane shows the linear TracePane for this session
   *  as a takeover overlay, with a back-to-canvas link. */
  detailSessionId: string | null;
  /** When the detail overlay was opened from a windowed canvas node
   *  (an invoke/invoke_resume slice rather than the whole session),
   *  ``detailWindow`` records the slice's ``[start, end)`` so the trace
   *  shows only that range. ``null`` = show the full session. */
  detailWindow: { start: string | null; end: string | null } | null;
  /** Transient signal: when the user enters the canvas pane via
   *  keyboard (←/→), the canvas auto-focuses the root node. Mouse
   *  clicks must NOT trigger this auto-focus — otherwise a click on a
   *  specific node loses to the auto-focus race. The flag is consumed
   *  and cleared by the canvas's auto-focus effect. */
  canvasEnterIntent: "keyboard" | null;
  setSlug: (slug: string | null) => void;
  selectRootSession: (id: string | null) => void;
  selectThreadSession: (id: string | null) => void;
  setIncludeMeta: (v: boolean) => void;
  setSessionFilter: (v: string) => void;
  setShowForks: (v: boolean) => void;
  setCallsOnly: (v: boolean) => void;
  openDetail: (
    id: string,
    window?: { start: string | null; end: string | null } | null,
  ) => void;
  closeDetail: () => void;
  focusPane: (p: PaneKey) => void;
  rotateFocus: (dir: 1 | -1) => void;
  enterCanvasViaKeyboard: () => void;
  clearCanvasEnterIntent: () => void;
}

const PANE_ORDER: PaneKey[] = ["sessions", "thread"];

export const useUi = create<UiState>((set, get) => ({
  slug: null,
  rootSessionId: null,
  threadSessionId: null,
  includeMeta: false,
  sessionFilter: "",
  showForks: false,
  callsOnly: false,
  detailSessionId: null,
  detailWindow: null,
  canvasEnterIntent: null,
  focusedPane: "sessions",
  setSlug: (slug) =>
    set({
      slug,
      rootSessionId: null,
      threadSessionId: null,
      sessionFilter: "",
      detailSessionId: null,
      detailWindow: null,
    }),
  selectRootSession: (id) =>
    set({
      rootSessionId: id,
      threadSessionId: id,
      detailSessionId: null,
      detailWindow: null,
    }),
  selectThreadSession: (id) => set({ threadSessionId: id }),
  setIncludeMeta: (v) => set({ includeMeta: v }),
  setSessionFilter: (v) => set({ sessionFilter: v }),
  setShowForks: (v) => set({ showForks: v }),
  setCallsOnly: (v) => set({ callsOnly: v }),
  openDetail: (id, window) =>
    set({ detailSessionId: id, detailWindow: window ?? null }),
  closeDetail: () => set({ detailSessionId: null, detailWindow: null }),
  focusPane: (p) => set({ focusedPane: p }),
  rotateFocus: (dir) => {
    // Clamp at the ends rather than wrapping — pressing ← from the
    // leftmost pane (or → from the rightmost) should be a no-op, not
    // jump to the opposite side of the layout.
    const cur = get().focusedPane;
    const i = PANE_ORDER.indexOf(cur);
    const next = Math.max(0, Math.min(PANE_ORDER.length - 1, i + dir));
    if (next === i) return;
    const nextPane = PANE_ORDER[next];
    set({
      focusedPane: nextPane,
      // Signal to the canvas auto-focus effect that this transition came
      // from a keyboard rotation — clicks (which call ``focusPane``
      // directly) leave the intent ``null`` so the click handler runs
      // unopposed.
      canvasEnterIntent: nextPane === "thread" ? "keyboard" : get().canvasEnterIntent,
    });
  },
  enterCanvasViaKeyboard: () =>
    set({ focusedPane: "thread", canvasEnterIntent: "keyboard" }),
  clearCanvasEnterIntent: () => set({ canvasEnterIntent: null }),
}));
