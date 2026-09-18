import { ACTIVITY_PHASE_LABEL, ACTIVITY_TYPE_LABEL, Tag } from "../shared/StatusTag";
import { useStoryActivity, type ActivityConnection } from "../../hooks/useStories";
import { cn } from "../../lib/utils";

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

// Live activity feed for the story detail dialog. `enabled` gates the SSE
// connection itself — the hook opens it when the dialog opens and closes
// it on unmount, so there's no page-global "is the dialog still open" flag
// and no interval to keep in sync.
export function ActivityFeed({ storyKey, enabled }: { storyKey: string; enabled: boolean }) {
  const { events, connection } = useStoryActivity(storyKey, enabled);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">Live activity</span>
        <ConnectionStatus connection={connection} />
      </div>
      <div className="flex max-h-[280px] flex-col gap-2.5 overflow-y-auto" aria-live="polite">
        {events.length === 0 ? (
          <p className="text-muted-foreground text-xs">No activity recorded yet for this ticket's current run.</p>
        ) : (
          events
            .slice()
            .reverse()
            .map((ev) => (
              // Keyed on the server-side event id, not the array index:
              // events arrive at the top of a reversed list, so index keys
              // would make React re-label every row on each new event.
              <div
                className={cn("border-border border-l-2 pl-2.5", ev.type === "error" && "border-destructive")}
                key={ev.id}
              >
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
    </div>
  );
}
