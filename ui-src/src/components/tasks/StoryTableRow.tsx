import {
  ClosedExternallyTag,
  CodeWrittenTag,
  CoverageGapTag,
  ExternalChangedTag,
  GATE_ACTION,
  KindTag,
  MergeAttemptsTag,
  PARKED_REASON_LABEL,
  PriorWorkTag,
  ReadOnlyChangesTag,
  StatusTagFor,
  unmetDependencies,
} from "../shared/StatusTag";
import { CancelRunButton } from "../project-detail/CancelRunButton";
import { PriorWorkChoiceButton } from "../project-detail/PriorWorkChoice";
import { needsChoice } from "../../lib/priorWork";
import { useApproveStory, useRetryStory, useStartStory } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { displayKey } from "../../lib/ticketSource";
import type { Story } from "../../types/api";
import { Button } from "../ui/button";

interface StoryTableRowProps {
  projectId: number;
  story: Story;
  // Every story's status in the epic, by key — what "waits for …" reads.
  statusByKey: Map<string, string>;
  autoRunEnabled: boolean;
  onOpen: (key: string) => void;
}

// One top-level story in an expanded epic: wave, key, title (and its branch
// while it has one), what is notable about it, its status, and at most one
// action — the one that moves it on. Everything else is in the drawer the
// row opens.
export function StoryTableRow({ projectId, story, statusByKey, autoRunEnabled, onOpen }: StoryTableRowProps) {
  const startStory = useStartStory(projectId);
  const approveStory = useApproveStory(projectId);
  const retryStory = useRetryStory(projectId);
  const { showToast } = useToast();

  const gate = GATE_ACTION[story.status];
  const waitingOn = unmetDependencies(story, statusByKey);
  // Several diverged branches and no pick yet: the daemon would refuse to
  // start it, so Start gives way to Choose branch.
  const choosing = story.status === "pending" && needsChoice(story.prior_work).length > 0;
  const label = displayKey(story);

  function action() {
    if (gate) {
      if (story.gate_approved) return <span className="text-muted-foreground text-xs whitespace-nowrap">{gate.approved}</span>;
      // The planner is waiting on a decision, not a plain plan review —
      // answering means picking an option per question, in the drawer.
      if (story.clarification_questions.length) {
        return (
          <Button size="sm" variant="outline" onClick={() => onOpen(story.jira_key)}>
            Answer {story.clarification_questions.length === 1 ? "question" : `${story.clarification_questions.length} questions`}
          </Button>
        );
      }
      return (
        <Button
          size="sm"
          title={gate.hint}
          aria-label={`${gate.label} — ${label}`}
          loading={approveStory.isPending}
          onClick={() => approveStory.mutate(story.jira_key)}
        >
          {gate.label}
        </Button>
      );
    }
    if (story.status === "failed") {
      return (
        <Button
          size="sm"
          variant="outline"
          loading={retryStory.isPending}
          onClick={() =>
            retryStory.mutate(story.jira_key, {
              onSuccess: () => showToast(`${label} queued for retry`, "success"),
              onError: (err) => showToast(err.message || "Could not retry this story.", "error"),
            })
          }
        >
          Retry
        </Button>
      );
    }
    if (story.status === "dispatched") return <CancelRunButton projectId={projectId} storyKey={story.jira_key} label={label} />;
    if (choosing) return <PriorWorkChoiceButton projectId={projectId} story={story} />;
    // Manual-run projects (auto_run_enabled off) need an explicit tap to
    // dispatch a pending story — nothing autopicks it — unless it still waits
    // on a dependency, which the daemon would refuse to start.
    if (!autoRunEnabled && story.status === "pending" && !waitingOn.length) {
      return (
        <Button
          size="sm"
          loading={startStory.isPending}
          onClick={() =>
            startStory.mutate(story.jira_key, {
              onSuccess: () => showToast(`${label} started`, "success"),
              onError: (err) => showToast(err.message || "Could not start this story.", "error"),
            })
          }
        >
          Start
        </Button>
      );
    }
    return null;
  }

  // A gate reason (plan_approval_gate) only repeats what the pill and the
  // button say; any other reason is new information.
  const reason = story.parked_reason && !story.parked_reason.endsWith("_gate") ? PARKED_REASON_LABEL[story.parked_reason] ?? story.parked_reason : null;

  return (
    <div
      className="hover:bg-accent/50 group flex min-w-0 cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:flex-nowrap sm:pl-14"
      onClick={(e) => {
        // Anything interactive inside the row handles its own click; without
        // this guard, approving a story would also open it.
        if ((e.target as HTMLElement).closest("button, a, select, input, [role='combobox'], [role='dialog']")) return;
        onOpen(story.jira_key);
      }}
    >
      <span className="text-muted-foreground w-7 shrink-0 text-xs tabular-nums">W{story.wave}</span>
      <button
        type="button"
        onClick={() => onOpen(story.jira_key)}
        className="shrink-0 font-mono text-sm hover:underline"
        aria-label={`Open ${label}`}
      >
        {label}
      </button>
      <div className="flex min-w-0 flex-1 basis-48 items-baseline gap-2">
        <span className="truncate text-sm">{story.summary}</span>
        {story.branch && story.status === "dispatched" ? (
          <span className="text-muted-foreground hidden truncate font-mono text-xs lg:inline">{story.branch}</span>
        ) : null}
        {reason ? <span className="text-muted-foreground hidden shrink-0 text-xs md:inline">({reason})</span> : null}
      </div>
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
        <KindTag story={story} />
        <CoverageGapTag story={story} />
        <ReadOnlyChangesTag story={story} />
        <PriorWorkTag story={story} />
        <MergeAttemptsTag story={story} />
        <CodeWrittenTag story={story} />
        <ExternalChangedTag story={story} />
        <ClosedExternallyTag story={story} />
        {waitingOn.length ? (
          <span className="bg-muted text-foreground rounded-md px-2 py-0.5 text-xs" title="Starts once these have merged into the epic branch">
            waits for {waitingOn.join(", ")}
          </span>
        ) : (
          <StatusTagFor status={story.status} />
        )}
        {action()}
      </div>
    </div>
  );
}
