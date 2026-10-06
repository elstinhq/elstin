import * as React from "react";
import { Brain, ChevronRight } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../ui/collapsible";
import { cn } from "../../lib/utils";

// AI Elements' Reasoning: the agent's thinking as a collapsed one-liner that
// opens on demand. While `streaming` it is open and labelled "Thinking…" so
// the live edge of a run is visible without reading every block.
export function Reasoning({
  title,
  streaming,
  time,
  children,
}: {
  title: string;
  streaming?: boolean;
  time?: string;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  const expanded = open || !!streaming;
  return (
    <Collapsible open={expanded} onOpenChange={setOpen}>
      <CollapsibleTrigger className="text-muted-foreground hover:text-foreground group flex w-full items-center gap-2 text-left text-xs">
        <Brain className={cn("size-3.5 shrink-0", streaming && "animate-pulse motion-reduce:animate-none")} aria-hidden="true" />
        <span className="truncate">{streaming ? "Thinking…" : title || "Thought"}</span>
        {time ? <span className="ml-auto shrink-0 tabular-nums">{time}</span> : null}
        <ChevronRight className="size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90" aria-hidden="true" />
      </CollapsibleTrigger>
      {children ? (
        <CollapsibleContent>
          <div className="border-border text-muted-foreground mt-1 ml-1.5 whitespace-pre-wrap border-l-2 pl-3 text-xs">{children}</div>
        </CollapsibleContent>
      ) : null}
    </Collapsible>
  );
}
