import * as React from "react";
import { cn } from "@/lib/utils";

type Variant = "default" | "outline" | "secondary" | "success" | "warn" | "error";

const variantClasses: Record<Variant, string> = {
  default: "bg-primary text-primary-foreground",
  outline: "border border-border text-foreground",
  secondary: "bg-secondary text-secondary-foreground",
  success: "bg-emerald-600/20 text-emerald-300 border border-emerald-900/40",
  warn: "bg-amber-600/20 text-amber-300 border border-amber-900/40",
  error: "bg-red-600/20 text-red-300 border border-red-900/40",
};

export function Badge({
  className,
  variant = "outline",
  ...rest
}: React.HTMLAttributes<HTMLSpanElement> & { variant?: Variant }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        variantClasses[variant],
        className,
      )}
      {...rest}
    />
  );
}
