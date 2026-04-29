import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Compact form, e.g. "12s", "5m", "3h", "2d". */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const now = Date.now();
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const delta = Math.max(0, now - then);
  const s = Math.floor(delta / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d`;
}

/**
 * Readable form, e.g. "just now", "12s ago", "1h 10min ago", "3d ago".
 * Use for session metadata shown to the user as prose.
 */
export function formatTimeAgo(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const now = Date.now();
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const delta = Math.max(0, now - then);
  const s = Math.floor(delta / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) {
    const remS = s % 60;
    if (m < 10 && remS >= 5) return `${m}min ${remS}s ago`;
    return `${m}min ago`;
  }
  const h = Math.floor(m / 60);
  const remM = m % 60;
  if (h < 24) {
    if (remM === 0) return `${h}h ago`;
    return `${h}h ${remM}min ago`;
  }
  const d = Math.floor(h / 24);
  const remH = h % 24;
  if (d < 7) {
    if (remH === 0) return `${d}d ago`;
    return `${d}d ${remH}h ago`;
  }
  const w = Math.floor(d / 7);
  return `${w}w ago`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m ${s}s`;
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—";
  return id.slice(0, 8);
}
