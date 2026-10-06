import { AlertCircle, Cpu } from "lucide-react";
import type { ActivityEvent } from "../../types/api";
import { Conversation } from "../ai-elements/conversation";
import { Reasoning } from "../ai-elements/reasoning";
import { Task } from "../ai-elements/task";
import { Tool } from "../ai-elements/tool";
import { ACTIVITY_PHASE_LABEL } from "../shared/StatusTag";
import { cn } from "../../lib/utils";

interface Group {
  phase: ActivityEvent["phase"];
  events: ActivityEvent[];
}

// Consecutive events from the same phase read as one task ("Dev — 14
// steps"); a phase change starts the next. Events arrive id-ascending.
function groupByPhase(events: ActivityEvent[]): Group[] {
  const groups: Group[] = [];
  for (const ev of events) {
    const last = groups[groups.length - 1];
    if (last && last.phase === ev.phase) last.events.push(ev);
    else groups.push({ phase: ev.phase, events: [ev] });
  }
  return groups;
}

const time = (ts: string) => (ts ? new Date(ts).toLocaleTimeString() : "");

function Step({ ev, live }: { ev: ActivityEvent; live: boolean }) {
  switch (ev.type) {
    case "thinking":
      return (
        <Reasoning title={ev.title} streaming={live} time={time(ev.ts)}>
          {ev.detail}
        </Reasoning>
      );
    case "tool_use":
      return (
        <Tool title={ev.title} time={time(ev.ts)}>
          {ev.detail}
        </Tool>
      );
    case "llm_call":
      return (
        <div className="text-muted-foreground flex items-center gap-2 text-xs">
          <Cpu className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{ev.title}</span>
          <span className="ml-auto shrink-0 tabular-nums">{time(ev.ts)}</span>
        </div>
      );
    case "error":
      return (
        <div className="border-destructive/40 bg-destructive/5 rounded-md border px-2.5 py-1.5">
          <div className="text-destructive flex items-center gap-2 text-xs font-medium">
            <AlertCircle className="size-3.5 shrink-0" aria-hidden="true" />
            <span>{ev.title}</span>
            <span className="ml-auto shrink-0 tabular-nums font-normal">{time(ev.ts)}</span>
          </div>
          {ev.detail ? <div className="text-muted-foreground mt-1 whitespace-pre-wrap text-xs">{ev.detail}</div> : null}
        </div>
      );
    default:
      // status
      return (
        <div className="text-sm">
          <div className="flex items-baseline gap-2">
            <span className="font-medium">{ev.title}</span>
            <span className="text-muted-foreground ml-auto shrink-0 text-xs tabular-nums">{time(ev.ts)}</span>
          </div>
          {ev.detail ? <div className="text-muted-foreground mt-0.5 whitespace-pre-wrap text-xs">{ev.detail}</div> : null}
        </div>
      );
  }
}

// The timeline view of the live feed: oldest first, following the newest
// event while the reader is at the bottom (the classic list puts newest on
// top). `live` marks the final thinking block as still streaming.
export function ActivityTimeline({
  events,
  live,
  emptyText,
  maxHeightClass,
}: {
  events: ActivityEvent[];
  live: boolean;
  emptyText: string;
  maxHeightClass: string;
}) {
  if (events.length === 0) return <p className="text-muted-foreground text-xs">{emptyText}</p>;
  const groups = groupByPhase(events);
  const newestId = events[events.length - 1]!.id;
  return (
    <Conversation version={newestId} className={cn("flex flex-col gap-3 pr-1", maxHeightClass)}>
      {groups.map((g, i) => {
        const isLast = i === groups.length - 1;
        return (
          <Task
            // First event id is stable for a group even as it grows.
            key={g.events[0]!.id}
            title={(g.phase && ACTIVITY_PHASE_LABEL[g.phase]) || "Activity"}
            meta={`${g.events.length} ${g.events.length === 1 ? "step" : "steps"}`}
            defaultOpen={isLast}
          >
            {g.events.map((ev) => (
              <Step key={ev.id} ev={ev} live={live && isLast && ev.id === newestId && ev.type === "thinking"} />
            ))}
          </Task>
        );
      })}
    </Conversation>
  );
}
