import { useSources } from "@/api/client";

export function SourceFilter({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (source: string) => void;
  label: string;
}) {
  const { data: sources } = useSources();
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="h-10 max-w-full rounded-lg border border-border bg-background px-2 text-xs text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
    >
      <option value="all">All sources</option>
      {sources?.map((source) => (
        <option key={source.id} value={source.id}>
          {source.label}
        </option>
      ))}
    </select>
  );
}
