import { STATUS_GROUPS, STATUS_GROUP_LABEL, STATUS_GROUP_SWATCH, STATUS_LABEL, storyGroup } from "../../lib/status";
import { displayKey } from "../../lib/ticketSource";
import { cn } from "../../lib/utils";
import type { Story } from "../../types/api";

// One segment per top-level story, in the order they run, coloured by the
// story's group — the same colours as the legend, the pills and "Needs you".
// Sub-tasks are never dispatched and never finish, so they are not counted.
export function StoryProgress({ stories, className }: { stories: Story[]; className?: string }) {
  const done = stories.filter((s) => storyGroup(s) === "merged").length;
  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <div
        className="flex h-2 min-w-0 flex-1 gap-1"
        role="img"
        aria-label={`${done} of ${stories.length} stories merged`}
      >
        {stories.length === 0 ? (
          <span className="bg-muted-foreground/15 flex-1 rounded-full" />
        ) : (
          stories.map((story) => (
            <span
              key={story.jira_key}
              title={`${displayKey(story)} — ${story.summary || ""} (${STATUS_LABEL[story.status] ?? story.status})`}
              className={cn("min-w-1 flex-1 rounded-full", STATUS_GROUP_SWATCH[storyGroup(story)])}
            />
          ))
        )}
      </div>
      <span className="text-muted-foreground shrink-0 text-xs tabular-nums">
        {done}/{stories.length}
      </span>
    </div>
  );
}

// What the progress colours mean. "failed" appears only when something has.
export function StatusLegend({ showFailed = false }: { showFailed?: boolean }) {
  return (
    <ul className="text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1 text-xs" aria-label="Status colours">
      {STATUS_GROUPS.filter((g) => showFailed || g !== "failed").map((group) => (
        <li key={group} className="flex items-center gap-1.5">
          <span className={cn("size-2.5 rounded-sm", STATUS_GROUP_SWATCH[group])} aria-hidden="true" />
          {STATUS_GROUP_LABEL[group]}
        </li>
      ))}
    </ul>
  );
}
