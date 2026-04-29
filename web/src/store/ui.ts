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
  setSlug: (slug: string | null) => void;
  selectRootSession: (id: string | null) => void;
  selectThreadSession: (id: string | null) => void;
  setIncludeMeta: (v: boolean) => void;
  setSessionFilter: (v: string) => void;
  setShowForks: (v: boolean) => void;
  setCallsOnly: (v: boolean) => void;
  openDetail: (id: string) => void;
  closeDetail: () => void;
  focusPane: (p: PaneKey) => void;
  rotateFocus: (dir: 1 | -1) => void;
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
  focusedPane: "sessions",
  setSlug: (slug) =>
    set({
      slug,
      rootSessionId: null,
      threadSessionId: null,
      sessionFilter: "",
      detailSessionId: null,
    }),
  selectRootSession: (id) =>
    set({ rootSessionId: id, threadSessionId: id, detailSessionId: null }),
  selectThreadSession: (id) => set({ threadSessionId: id }),
  setIncludeMeta: (v) => set({ includeMeta: v }),
  setSessionFilter: (v) => set({ sessionFilter: v }),
  setShowForks: (v) => set({ showForks: v }),
  setCallsOnly: (v) => set({ callsOnly: v }),
  openDetail: (id) => set({ detailSessionId: id }),
  closeDetail: () => set({ detailSessionId: null }),
  focusPane: (p) => set({ focusedPane: p }),
  rotateFocus: (dir) => {
    const cur = get().focusedPane;
    const i = PANE_ORDER.indexOf(cur);
    const next = (i + dir + PANE_ORDER.length) % PANE_ORDER.length;
    set({ focusedPane: PANE_ORDER[next] });
  },
}));
