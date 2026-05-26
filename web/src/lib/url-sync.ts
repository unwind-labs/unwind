import { useEffect, useRef } from "react";
import { useUi } from "@/store/ui";

export type UrlState = {
  slug: string | null;
  rootSessionId: string | null;
  detailSessionId: string | null;
  detailWindow: { start: string | null; end: string | null } | null;
  canvasFocusedNodeId: string | null;
  reportsOpen: boolean;
  reportsMonth: string | null;
};

export function parseUrl(): UrlState {
  const params = new URLSearchParams(window.location.search);
  const slug = params.get("project");
  const rootSessionId = params.get("session");
  const detailSessionId = params.get("detail");
  const ws = params.get("ws");
  const we = params.get("we");
  const focus = params.get("focus");
  const detailWindow = detailSessionId && (ws || we) ? { start: ws, end: we } : null;
  const reportsOpen = params.get("view") === "reports";
  return {
    slug,
    rootSessionId,
    detailSessionId,
    detailWindow,
    canvasFocusedNodeId: focus,
    reportsOpen,
    // ``month`` only carries meaning while Reports is open.
    reportsMonth: reportsOpen ? params.get("month") : null,
  };
}

function buildSearch(state: UrlState): string {
  const params = new URLSearchParams();
  if (state.slug) params.set("project", state.slug);
  if (state.rootSessionId) params.set("session", state.rootSessionId);
  if (state.detailSessionId) {
    params.set("detail", state.detailSessionId);
    if (state.detailWindow?.start) params.set("ws", state.detailWindow.start);
    if (state.detailWindow?.end) params.set("we", state.detailWindow.end);
  }
  if (state.canvasFocusedNodeId) params.set("focus", state.canvasFocusedNodeId);
  if (state.reportsOpen) {
    params.set("view", "reports");
    if (state.reportsMonth) params.set("month", state.reportsMonth);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

// True while a popstate-driven apply is in flight, so that any store action
// fired indirectly during the apply (defensive) does NOT push a new entry on
// top of the one the browser just restored.
let isApplyingFromUrl = false;

function snapshot(): UrlState {
  const s = useUi.getState();
  return {
    slug: s.slug,
    rootSessionId: s.rootSessionId,
    detailSessionId: s.detailSessionId,
    detailWindow: s.detailWindow,
    canvasFocusedNodeId: s.canvasFocusedNodeId,
    reportsOpen: s.reportsOpen,
    reportsMonth: s.reportsMonth,
  };
}

function writeHistory(state: UrlState, mode: "push" | "replace", opts?: { force?: boolean }) {
  if (!opts?.force && isApplyingFromUrl) return;
  const target = window.location.pathname + buildSearch(state);
  const current = window.location.pathname + window.location.search;
  if (target === current) return;
  if (mode === "push") {
    window.history.pushState({}, "", target);
  } else {
    window.history.replaceState({}, "", target);
  }
}

type DetailWin = { start: string | null; end: string | null } | null | undefined;

export const navigate = {
  setSlug(slug: string | null) {
    useUi.getState().setSlug(slug);
    writeHistory(snapshot(), "push");
  },
  selectRootSession(id: string | null) {
    useUi.getState().selectRootSession(id);
    writeHistory(snapshot(), "push");
  },
  // Fired from SessionListPane's "no selection? pick the most recent"
  // effect — not a user nav, so canonicalize in place.
  selectRootSessionAuto(id: string | null) {
    useUi.getState().selectRootSession(id);
    writeHistory(snapshot(), "replace");
  },
  openDetail(id: string, win?: DetailWin) {
    useUi.getState().openDetail(id, win ?? null);
    writeHistory(snapshot(), "push");
  },
  // Single-window canvas auto-opens the trace overlay; not a user nav.
  openDetailAuto(id: string, win?: DetailWin) {
    useUi.getState().openDetail(id, win ?? null);
    writeHistory(snapshot(), "replace");
  },
  closeDetail() {
    useUi.getState().closeDetail();
    writeHistory(snapshot(), "push");
  },
  setCanvasFocus(id: string | null) {
    useUi.getState().setCanvasFocusedNodeId(id);
    writeHistory(snapshot(), "replace");
  },
  openReports() {
    useUi.getState().openReports();
    writeHistory(snapshot(), "push");
  },
  // Push so the back button returns to the pre-Reports view (params drop).
  closeReports() {
    useUi.getState().closeReports();
    writeHistory(snapshot(), "push");
  },
  // Month changes canonicalize in place (like canvas focus) so flipping
  // through months doesn't stack history entries — refresh still restores.
  setReportsMonth(month: string) {
    useUi.getState().setReportsMonth(month);
    writeHistory(snapshot(), "replace");
  },
};

/** Applies the current URL into the store on mount, installs popstate, and
 *  fills in the server's default project once it loads (only if no project
 *  is already selected — back-navigation to a no-project URL is honored). */
export function useUrlSync(defaultProjectSlug: string | null | undefined) {
  const settledRef = useRef(false);

  useEffect(() => {
    isApplyingFromUrl = true;
    try {
      useUi.getState().applyUrlState(parseUrl());
    } finally {
      isApplyingFromUrl = false;
    }
    if (useUi.getState().slug) settledRef.current = true;
    // Canonicalize: write back the parsed URL so e.g. param order is stable.
    // Force past isApplyingFromUrl since we're still inside the apply window.
    writeHistory(snapshot(), "replace", { force: true });

    const onPop = () => {
      isApplyingFromUrl = true;
      try {
        useUi.getState().applyUrlState(parseUrl());
      } finally {
        isApplyingFromUrl = false;
      }
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (settledRef.current) return;
    if (!defaultProjectSlug) return;
    if (useUi.getState().slug) {
      settledRef.current = true;
      return;
    }
    settledRef.current = true;
    isApplyingFromUrl = true;
    try {
      useUi.getState().applyUrlState({
        ...snapshot(),
        slug: defaultProjectSlug,
      });
    } finally {
      isApplyingFromUrl = false;
    }
    writeHistory(snapshot(), "replace", { force: true });
  }, [defaultProjectSlug]);
}
