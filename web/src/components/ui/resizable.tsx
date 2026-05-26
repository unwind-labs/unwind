import {
  PanelGroup,
  Panel,
  PanelResizeHandle,
  type PanelGroupProps,
  type PanelProps,
} from "react-resizable-panels";
import { cn } from "@/lib/utils";

export function ResizablePanelGroup(props: PanelGroupProps) {
  return <PanelGroup {...props} className={cn("h-full w-full", props.className)} />;
}

export function ResizablePanel(props: PanelProps) {
  return <Panel {...props} />;
}

export function ResizableHandle({ className }: { className?: string }) {
  return (
    <PanelResizeHandle
      className={cn(
        "relative w-px bg-border data-[resize-handle-state=hover]:bg-primary/40 data-[resize-handle-state=drag]:bg-primary/60 transition-colors",
        className,
      )}
    />
  );
}
