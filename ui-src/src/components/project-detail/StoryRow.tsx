import { ClosedExternallyTag, CoverageGapTag, ExternalChangedTag, KindTag, PrOpenErrorTag, StatusTagFor } from "../shared/StatusTag";
import { Twisty } from "../shared/Twisty";
import { useApproveStory, useRetryStory, useStartStory } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import type { Story } from "../../types/api";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";
import { displayKey } from "../../lib/ticketSource";

interface StoryRowProps {
  projectId: number;
  story: Story;
  subtasks?: Story[];
  isSubtask?: boolean;
  autoRunEnabled: boolean;
  expandedStories: Set<string>;
  onToggleExpand: (key: string) => void;
  onOpenStory: (key: string) => void;
}

export function StoryRow({
  projectId,
  story,
  subtasks = [],
  isSubtask = false,
  autoRunEnabled,
  expandedStories,
  onToggleExpand,
  onOpenStory,
}: StoryRowProps) {
  const startStory = useStartStory(projectId);
  const approveStory = useApproveStory(projectId);
  const retryStory = useRetryStory(projectId);
  const { showToast } = useToast();

  const isOpen = expandedStories.has(story.jira_key);
  const parked =
    story.status === "awaiting_plan_approval" || story.status === "awaiting_pr_open" || story.status === "awaiting_pr_review";

  return (
    <>
      <div
        className={cn(
          "border-border flex min-w-0 flex-nowrap items-center gap-3 border-b py-2.5 last:border-b-0",
          "hover:bg-accent/50 -mx-2 cursor-pointer rounded-md px-2",
          isSubtask && "border-border/60 ml-[9px] border-l pl-6",
        )}
        onClick={(e) => {
          const target = e.target as HTMLElement;
          // Anything interactive inside the row handles its own click;
          // without this guard, approving a story would also open it.
          if (target.closest("button, a, select, input, [role='combobox']")) return;
          onOpenStory(story.jira_key);
        }}
      >
        {subtasks.length ? (
          <Twisty isOpen={isOpen} onToggle={() => onToggleExpand(story.jira_key)} />
        ) : (
          <span className="w-4 shrink-0" />
        )}

        <span className={cn("shrink-0 font-mono text-sm", isSubtask ? "min-w-[70px] opacity-75" : "min-w-[78px]")}>
          {displayKey(story)}
        </span>
        <KindTag story={story} />
        <CoverageGapTag story={story} />
        <PrOpenErrorTag story={story} />
        <StatusTagFor status={story.status} />
        <ExternalChangedTag story={story} />
        <ClosedExternallyTag story={story} />

        {story.parked_reason ? <span className="text-muted-foreground text-xs">({story.parked_reason})</span> : null}

        <span className="text-muted-foreground w-[34px] shrink-0 text-xs">{`W${story.wave}`}</span>

        {/* A sub-task never gets a branch, so that slot would always be
            empty for one. Its summary is the useful thing to show there
            instead — it's what tells you what the breakdown actually
            covers without opening each one. */}
        {story.branch || isSubtask ? (
          <span className="text-muted-foreground min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-mono text-xs">
            {story.branch || story.summary || ""}
          </span>
        ) : (
          <span className="flex-1" />
        )}

        {story.pull_request && story.pull_request.number ? (
          <Button variant="outline" size="sm" className="shrink-0" asChild>
            <a href={story.pull_request.url || "#"} target="_blank" rel="noreferrer">
              #{story.pull_request.number} ({story.pull_request.state})
            </a>
          </Button>
        ) : null}

        {/* Manual-run projects (auto_run_enabled off) need an explicit tap
            to dispatch a pending top-level story — nothing autopicks it.
            Sub-tasks are never dispatched on their own (see AGENTS.md), so
            they never get this button even though their status can also be
            "pending". */}
        {!autoRunEnabled && story.status === "pending" && !isSubtask ? (
          <Button
            size="sm"
            className="shrink-0"
            loading={startStory.isPending}
            onClick={() =>
              startStory.mutate(story.jira_key, {
                onSuccess: () => showToast(`${story.jira_key} started`, "success"),
                onError: (err) => showToast(err.message || "Could not start this story.", "error"),
              })
            }
          >
            Start
          </Button>
        ) : null}

        {parked ? (
          story.gate_approved ? (
            <span className="text-muted-foreground shrink-0 whitespace-nowrap text-xs">approved — waiting</span>
          ) : (
            <Button size="sm" className="shrink-0" loading={approveStory.isPending} onClick={() => approveStory.mutate(story.jira_key)}>
              Approve
            </Button>
          )
        ) : story.status === "failed" ? (
          <Button
            size="sm"
            className="shrink-0"
            loading={retryStory.isPending}
            onClick={() =>
              retryStory.mutate(story.jira_key, {
                onSuccess: () => showToast(`${story.jira_key} queued for retry`, "success"),
                onError: (err) => showToast(err.message || "Could not retry this story.", "error"),
              })
            }
          >
            Retry
          </Button>
        ) : null}
      </div>
      {subtasks.length ? (
        <div className={cn(isOpen ? "block" : "hidden")}>
          {subtasks.map((subtask) => (
            <StoryRow
              key={subtask.jira_key}
              projectId={projectId}
              story={subtask}
              isSubtask
              autoRunEnabled={autoRunEnabled}
              expandedStories={expandedStories}
              onToggleExpand={onToggleExpand}
              onOpenStory={onOpenStory}
            />
          ))}
        </div>
      ) : null}
    </>
  );
}
