/**
 * True when a keyboard event's target is a text-input surface and global
 * shortcut handlers should yield (no preventDefault, no stopPropagation).
 *
 * Three pane-level keydown listeners independently duplicated this check.
 * Pulled into one helper so adding e.g. <select> or shadow-DOM editors in
 * the future only touches one place.
 */
export function isTypingTarget(e: Event): boolean {
  const target = e.target as HTMLElement | null;
  if (!target) return false;
  return (
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.isContentEditable
  );
}
