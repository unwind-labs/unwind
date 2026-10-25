import { Bot } from "lucide-react";

/** Provider identity stays separate from the session's activity indicator. */
export function ProviderIcon({
  provider,
  className = "h-4 w-4 shrink-0",
}: {
  provider: string;
  className?: string;
}) {
  if (provider === "claude") {
    return (
      <svg
        viewBox="0 0 24 24"
        className={`${className} text-orange-300`}
        role="img"
        aria-label="Claude Code"
      >
        <title>Claude Code</title>
        <g stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          {Array.from({ length: 12 }, (_, i) => (
            <path key={i} d="M12 3v6" transform={`rotate(${i * 30} 12 12)`} />
          ))}
        </g>
      </svg>
    );
  }
  if (provider === "codex") {
    return (
      <svg viewBox="0 0 24 24" className={className} role="img" aria-label="Codex / ChatGPT">
        <title>Codex / ChatGPT</title>
        <g fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
          {Array.from({ length: 6 }, (_, i) => (
            <path
              key={i}
              d="M12 8.5V3.7C17 1 22 6 19.5 10.5L15 13"
              transform={`rotate(${i * 60} 12 12)`}
            />
          ))}
        </g>
      </svg>
    );
  }
  return <Bot className={className} aria-label={provider} />;
}
