import * as React from "react";
import { ChevronRight } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../ui/collapsible";

// AI Elements' Task: a titled, collapsible group of steps. Here one task is
// a run of consecutive events from the same agent phase (planner, dev, …).
export function Task({
  title,
  meta,
  defaultOpen,
  children,
}: {
  title: string;
  meta?: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  return (
    <Collapsible defaultOpen={defaultOpen} className="flex flex-col gap-2">
      <CollapsibleTrigger className="group flex w-full items-center gap-2 text-left">
        <ChevronRight className="text-muted-foreground size-4 shrink-0 transition-transform group-data-[state=open]:rotate-90" aria-hidden="true" />
        <span className="text-sm font-medium">{title}</span>
        {meta ? <span className="text-muted-foreground text-xs">{meta}</span> : null}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="border-border ml-2 flex flex-col gap-2 border-l pl-4">{children}</div>
      </CollapsibleContent>
    </Collapsible>
  );
}
