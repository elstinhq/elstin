import type { LucideIcon } from "lucide-react";

interface EmptyStateProps {
  icon: LucideIcon;
  heading: string;
  body: string;
}

export function EmptyState({ icon: Icon, heading, body }: EmptyStateProps) {
  return (
    <div className="border-border text-muted-foreground flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed py-14 px-6 text-center">
      <Icon className="text-muted-foreground size-6" />
      <p className="text-foreground text-sm font-medium">{heading}</p>
      <p className="text-xs">{body}</p>
    </div>
  );
}
