import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { Message, MessagesResponse, SessionRow } from "@/api/types";

type WsEvent =
  | { type: "ready"; slug: string }
  | { type: "pong" }
  | {
      type: "session_created";
      slug: string;
      session_id: string;
      summary: SessionRow | null;
    }
  | {
      type: "session_updated";
      slug: string;
      session_id: string;
      summary: SessionRow | null;
    }
  | {
      type: "messages_appended";
      slug: string;
      session_id: string;
      messages: Message[];
      file_offset: number;
    }
  | { type: "tree_changed"; slug: string }
  | { type: string; [k: string]: unknown };

/**
 * Subscribes to `/api/ws?project=<slug>`, reconnects on drop, and patches the
 * TanStack Query cache so panes re-render without polling.
 */
export function useLiveEvents(slug: string | null | undefined) {
  const qc = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<number | null>(null);

  useEffect(() => {
    if (!slug) return;

    let cancelled = false;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const url = `${scheme}://${location.host}/api/ws?project=${encodeURIComponent(slug)}`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        pingRef.current = window.setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 25_000);
      };

      ws.onmessage = (raw) => {
        let ev: WsEvent;
        try {
          ev = JSON.parse(raw.data);
        } catch {
          return;
        }
        handleEvent(qc, slug, ev);
      };

      ws.onerror = () => {
        /* handled via onclose */
      };

      ws.onclose = () => {
        if (pingRef.current !== null) {
          window.clearInterval(pingRef.current);
          pingRef.current = null;
        }
        if (cancelled) return;
        attempt = Math.min(attempt + 1, 6);
        const delay = Math.min(500 * 2 ** attempt, 10_000);
        window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (pingRef.current !== null) window.clearInterval(pingRef.current);
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          /* ignore */
        }
      }
    };
  }, [slug, qc]);
}

function handleEvent(
  qc: ReturnType<typeof useQueryClient>,
  slug: string,
  ev: WsEvent,
) {
  switch (ev.type) {
    case "session_created": {
      // Don't optimistically insert: at creation time, the heuristic may not
      // yet have signal to classify a brand-new session as a fork. An eager
      // insert causes the row to flash and disappear when the next refetch
      // applies fork filtering. Invalidate; the next fetch is authoritative.
      qc.invalidateQueries({ queryKey: ["sessions", slug], exact: false });
      break;
    }
    case "session_updated": {
      const { session_id, summary } = ev as Extract<WsEvent, { type: "session_updated" }>;
      if (!summary) break;
      qc.setQueriesData<SessionRow[]>({ queryKey: ["sessions", slug] }, (prev) => {
        const list = prev ?? [];
        const next = list.map((s) =>
          s.session_id === session_id ? { ...s, ...summary } : s,
        );
        next.sort(
          (a, b) =>
            (b.last_timestamp ? Date.parse(b.last_timestamp) : 0) -
            (a.last_timestamp ? Date.parse(a.last_timestamp) : 0),
        );
        return next;
      });
      break;
    }
    case "messages_appended": {
      const { session_id, messages } = ev as Extract<
        WsEvent,
        { type: "messages_appended" }
      >;
      // Patch both include_meta=false and =true caches if they exist.
      for (const meta of [false, true]) {
        qc.setQueryData<MessagesResponse>(
          ["messages", slug, session_id, meta],
          (prev) => {
            if (!prev) return prev;
            const seen = new Set(prev.messages.map((m) => m.uuid));
            const merged = [...prev.messages];
            for (const m of messages) {
              if (!seen.has(m.uuid)) {
                merged.push(m);
                seen.add(m.uuid);
              }
            }
            return {
              ...prev,
              messages: merged,
              last_uuid:
                merged.length > 0 ? merged[merged.length - 1].uuid : prev.last_uuid,
            };
          },
        );
      }
      break;
    }
    case "tree_changed": {
      // A new callstack report wrote: sessions list needs a fresh server view
      // since fork-classification only happens after report.yaml exists.
      qc.invalidateQueries({ queryKey: ["sessions", slug], exact: false });
      // Also invalidate any open child traces — their spawn metadata may have
      // changed (new resolved children, etc.).
      qc.invalidateQueries({ queryKey: ["messages", slug], exact: false });
      break;
    }
    default:
      break;
  }
}
