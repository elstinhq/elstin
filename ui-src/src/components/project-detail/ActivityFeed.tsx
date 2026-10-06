import { ChevronRight } from "lucide-react";
import { ACTIVITY_PHASE_LABEL, ACTIVITY_TYPE_LABEL, Tag } from "../shared/StatusTag";
import { useActivityStream, type ActivityConnection } from "../../hooks/useStories";
import { cn } from "../../lib/utils";
import { ActivityTimeline } from "./ActivityTimeline";
import { useState } from "react";

const CONNECTION_LABEL: Record<ActivityConnection, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  closed: "Disconnected",
};

const CONNECTION_DOT: Record<ActivityConnection, string> = {
  connecting: "bg-border",
  live: "bg-success animate-pulse motion-reduce:animate-none",
  reconnecting: "bg-warning",
  closed: "bg-destructive",
};

// Shown next to the heading so a silent feed is never ambiguous: with a
// push stream, "nothing new for two minutes" and "the connection died"
// look identical unless the connection state is on screen.
function ConnectionStatus({ connection }: { connection: ActivityConnection }) {
  return (
    <span
      className="text-muted-foreground inline-flex items-center gap-1.5 text-xs"
      title={connection === "live" ? "Streaming events as they happen" : CONNECTION_LABEL[connection]}
    >
      <span className={cn("size-1.5 shrink-0 rounded-full", CONNECTION_DOT[connection])} aria-hidden="true" />
      {CONNECTION_LABEL[connection]}
    </span>
  );
}

// Live activity feed — for a story's detail dialog and for a PO session's
// (`streamUrl` is the only difference). `enabled` gates the SSE connection
// itself — the hook opens it when the dialog opens and closes it on unmount,
// so there's no page-global "is the dialog still open" flag and no interval
// to keep in sync.
//
// `collapsible` folds it to one line — the connection, how many events, the latest
// — that opens to the timeline. The stream stays connected while it is folded, so
// that line is still live.
export function ActivityFeed({
  streamUrl,
  enabled,
  emptyText = "No activity recorded yet for this ticket's current run.",
  maxHeightClass = "max-h-[280px]",
  collapsible = false,
}: {
  streamUrl: string;
  enabled: boolean;
  emptyText?: string;
  maxHeightClass?: string;
  collapsible?: boolean;
}) {
  const { events, connection } = useActivityStream(streamUrl, enabled);
  const [view, setView] = useState<"timeline" | "list">("timeline");
  const [open, setOpen] = useState(!collapsible);
  const latest = events[events.length - 1];

  const layoutToggle = (
    <div className="flex gap-1 text-xs" role="group" aria-label="Feed layout">
      {(["timeline", "list"] as const).map((v) => (
        <button
          key={v}
          type="button"
          aria-pressed={view === v}
          onClick={() => setView(v)}
          className={cn("rounded px-1.5 py-0.5", view === v ? "bg-muted font-medium" : "text-muted-foreground hover:text-foreground")}
        >
          {v === "timeline" ? "Timeline" : "List"}
        </button>
      ))}
    </div>
  );

  const body =
    view === "timeline" ? (
      <ActivityTimeline events={events} live={connection === "live"} emptyText={emptyText} maxHeightClass={maxHeightClass} />
    ) : (
      <div className={cn("flex flex-col gap-2.5 overflow-y-auto", maxHeightClass)} aria-live="polite">
        {events.length === 0 ? (
          <p className="text-muted-foreground text-xs">{emptyText}</p>
        ) : (
          events
            .slice()
            .reverse()
            .map((ev) => (
              // Keyed on the server-side event id, not the array index:
              // events arrive at the top of a reversed list, so index keys
              // would make React re-label every row on each new event.
              <div className={cn("border-border border-l-2 pl-2.5", ev.type === "error" && "border-destructive")} key={ev.id}>
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-sm font-medium">{ev.title}</span>
                  {ev.phase ? <Tag label={ACTIVITY_PHASE_LABEL[ev.phase] || ev.phase} /> : null}
                  <span className="text-muted-foreground text-xs">{ACTIVITY_TYPE_LABEL[ev.type] || ev.type}</span>
                  <span className="text-muted-foreground text-xs">{ev.ts ? new Date(ev.ts).toLocaleTimeString() : ""}</span>
                </div>
                {ev.detail ? <div className="text-muted-foreground mt-0.5 whitespace-pre-wrap text-xs">{ev.detail}</div> : null}
              </div>
            ))
        )}
      </div>
    );

  if (collapsible) {
    return (
      <div className="bg-card rounded-xl border">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="hover:bg-accent/50 flex w-full min-w-0 items-center gap-2 rounded-xl px-3 py-2.5 text-left transition-colors"
        >
          <ChevronRight className={cn("text-muted-foreground size-4 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
          <span className="text-sm font-medium">Activity</span>
          <ConnectionStatus connection={connection} />
          {!open ? (
            <span className="text-muted-foreground ml-auto min-w-0 truncate text-xs">
              {latest ? `${events.length} ${events.length === 1 ? "step" : "steps"} · ${latest.title}` : "nothing yet"}
            </span>
          ) : null}
        </button>
        {open ? (
          <div className="flex flex-col gap-2 border-t px-3 py-2.5">
            <div className="flex justify-end">{layoutToggle}</div>
            {body}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">Live activity</span>
        <ConnectionStatus connection={connection} />
        <div className="ml-auto">{layoutToggle}</div>
      </div>
      {body}
    </div>
  );
}
