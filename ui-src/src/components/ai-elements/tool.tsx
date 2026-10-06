import * as React from "react";
import { ChevronRight, Wrench } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../ui/collapsible";

// AI Elements' Tool: one tool call as a bordered row, its arguments/result
// (the event's `detail`) behind a disclosure. Rows without a detail are not
// expandable. The stream carries no call/result pairing, so there is no
// running/completed state to badge — the row is simply "a tool was used".
export function Tool({ title, time, children }: { title: string; time?: string; children?: React.ReactNode }) {
  const header = (
    <>
      <Wrench className="text-muted-foreground size-3.5 shrink-0" aria-hidden="true" />
      <span className="truncate font-mono text-xs">{title}</span>
      {time ? <span className="text-muted-foreground ml-auto shrink-0 text-xs tabular-nums">{time}</span> : null}
    </>
  );
  if (!children) {
    return <div className="border-border bg-muted/30 flex items-center gap-2 rounded-md border px-2.5 py-1.5">{header}</div>;
  }
  return (
    <Collapsible className="border-border bg-muted/30 rounded-md border">
      <CollapsibleTrigger className="group flex w-full items-center gap-2 px-2.5 py-1.5 text-left">
        {header}
        {time ? null : <span className="ml-auto" />}
        <ChevronRight className="text-muted-foreground size-3.5 shrink-0 transition-transform group-data-[state=open]:rotate-90" aria-hidden="true" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre className="border-border text-muted-foreground max-h-48 overflow-auto whitespace-pre-wrap break-words border-t px-2.5 py-2 font-mono text-xs">
          {children}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}
