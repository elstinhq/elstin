import { useState } from "react";
import { ChevronRight, Ellipsis, GitBranch, Info, Network, Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { useSetEpicGate, useSyncEpicFields } from "../../hooks/useEpics";
import { displayKey, sourceLabel } from "../../lib/ticketSource";
import { useToast } from "../../lib/toast";
import { cn } from "../../lib/utils";
import type { Epic, ProjectRepo, Story } from "../../types/api";
import { PrApprovalPrompt } from "../project-detail/PrApprovalPrompt";
import { RemoveEpicDialog, epicRemovable } from "../project-detail/RemoveEpicDialog";
import { PhaseTagFor, Tag } from "../shared/StatusTag";
import { Button } from "../ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "../ui/dropdown-menu";
import { PriorWorkSummary } from "./PriorWorkSummary";
import { StoryProgress } from "./StoryProgress";
import { StoryTableRow } from "./StoryTableRow";

// Wave first, then issue key — the order the epic actually executes in. The
// API returns rows in arbitrary DB order, which put the QA ticket at the
// top of PROJ-3480 even though it runs last.
export const byWaveThenKey = (a: Story, b: Story) =>
  (a.wave ?? 0) - (b.wave ?? 0) || a.jira_key.localeCompare(b.jira_key, undefined, { numeric: true });

interface EpicRowProps {
  projectId: number;
  epic: Epic;
  repos: ProjectRepo[];
  expanded: boolean;
  onToggle: () => void;
  autoRunEnabled: boolean;
  onOpenEpic: (key: string, tab?: string) => void;
  onOpenStory: (key: string) => void;
}

export function EpicRow({ projectId, epic, repos, expanded, onToggle, autoRunEnabled, onOpenEpic, onOpenStory }: EpicRowProps) {
  const setEpicGate = useSetEpicGate(projectId);
  const syncFields = useSyncEpicFields(projectId);
  const { showToast } = useToast();
  const [removing, setRemoving] = useState(false);

  // Progress and the rows count top-level stories only. Sub-tasks are never
  // dispatched and never reach a done status (the drawer lists them).
  const stories = (epic.stories || []).filter((s) => !s.parent_key).sort(byWaveThenKey);
  const statusByKey = new Map(stories.map((s) => [s.jira_key, s.status as string]));
  const pullRequests = (epic.pull_requests || []).filter((pr) => pr.number || pr.error);
  // Pause/Resume is a hold on dispatch, and the daemon stops polling an epic
  // once it is merged or complete, so on a finished epic the flag does
  // nothing — yet it was left set from the epic's creation (epics start
  // paused) and read "Paused / Resume" on work that was long done.
  const finished = epic.phase === "merged" || epic.phase === "complete";
  const label = displayKey(epic);
  const removable = epicRemovable(epic);

  return (
    <div className="border-border border-b last:border-b-0">
      <div
        className="hover:bg-accent/40 flex min-w-0 cursor-pointer flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3.5"
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("button, a, [role='menu'], [role='dialog']")) return;
          onToggle();
        }}
      >
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${label}`}
          className="text-muted-foreground hover:text-foreground -m-1 rounded p-1"
        >
          <ChevronRight className={cn("size-4 transition-transform", expanded && "rotate-90")} />
        </button>
        <button
          type="button"
          onClick={() => onOpenEpic(epic.jira_key)}
          className="shrink-0 font-mono text-sm font-semibold hover:underline"
          aria-label={`Open ${label}`}
        >
          {label}
        </button>
        <div className="flex min-w-0 flex-1 basis-40 items-center gap-2">
          <span className="truncate text-sm font-medium">{epic.summary}</span>
          {pullRequests.map((pr) =>
            pr.number ? (
              <a
                key={pr.repo_label || "default"}
                href={pr.url || "#"}
                target="_blank"
                rel="noreferrer"
                className="shrink-0 text-sm underline underline-offset-4"
                title={`${pr.repo_label ? `${pr.repo_label}: ` : ""}${pr.state}${pr.ci_status ? `, CI ${pr.ci_status}` : ""}`}
              >
                {pr.repo_label ? `${pr.repo_label} ` : ""}#{pr.number}
              </a>
            ) : (
              <Tag key={pr.repo_label || "default"} label="PR not opened" appearance="destructive" title={pr.error || undefined} />
            ),
          )}
          {epic.manual_gate && !finished ? <Tag label="Paused" appearance="warning" /> : null}
          {epic.pr_approval === "awaiting" ? (
            <Tag label="PR waiting for you" appearance="warning" title="Every story has merged; nothing is pushed until you open the pull request or keep the branch local." />
          ) : null}
          {epic.external_closed ? (
            <Tag label="closed in source" appearance="secondary" title="The source reports this epic closed, so Elstin put it on hold. Resume it to keep going." />
          ) : null}
        </div>
        <div className="w-28 shrink-0">
          <PhaseTagFor phase={epic.phase} />
        </div>
        <StoryProgress stories={stories} className="hidden w-48 shrink-0 lg:flex" />
        <div className="hidden w-32 shrink-0 truncate text-sm xl:block">
          {epic.repo ? (
            <span
              className="text-muted-foreground"
              title={repos.length > 1 ? `Default repo for unprefixed paths. Every repo in this project (${repos.map((r) => r.label || r.path).join(", ")}) is readable and writable by the plan.` : epic.repo}
            >
              {epic.repo}
            </span>
          ) : repos.length === 0 ? (
            <Tag label="No folder" appearance="warning" title="Add a git repo in Folders before this epic can run." />
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {finished ? (
            <span className="w-[6.5rem]" />
          ) : (
            <Button
              variant="outline"
              size="sm"
              className="w-[6.5rem]"
              loading={setEpicGate.isPending}
              onClick={() => setEpicGate.mutate({ epicKey: epic.jira_key, manualGate: !epic.manual_gate })}
            >
              {setEpicGate.isPending ? null : epic.manual_gate ? <Play /> : <Pause />}
              {epic.manual_gate ? "Resume" : "Pause"}
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon" className="size-8" aria-label={`More for ${label}`}>
                <Ellipsis />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem onSelect={() => onOpenEpic(epic.jira_key)}>
                <Info />
                Details
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => onOpenEpic(epic.jira_key, "graph")} disabled={!stories.length}>
                <Network />
                Story graph
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() =>
                  syncFields.mutate(epic.jira_key, {
                    onSuccess: () => showToast(`Refreshed ${label} from ${sourceLabel(epic.external_source)}`, "success"),
                    onError: (err) => showToast(err.message || "Could not refresh", "error"),
                  })
                }
              >
                <RefreshCw />
                Refresh from {sourceLabel(epic.external_source)}
              </DropdownMenuItem>
              {epic.integration_branch ? (
                <DropdownMenuItem
                  onSelect={() => {
                    void navigator.clipboard?.writeText(epic.integration_branch ?? "");
                    showToast("Branch name copied", "success");
                  }}
                >
                  <GitBranch />
                  Copy branch name
                </DropdownMenuItem>
              ) : null}
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                disabled={!removable}
                title={removable ? undefined : "A run is in progress — cancel it or wait for it to finish first"}
                onSelect={() => setRemoving(true)}
              >
                <Trash2 />
                Remove from Elstin
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {expanded ? (
        <div className="bg-muted/30 border-border border-t">
          {epic.pr_approval === "awaiting" ? (
            <div className="px-4 pt-3 sm:pl-14">
              <PrApprovalPrompt projectId={projectId} epic={epic} />
            </div>
          ) : null}
          <div className="px-4 pt-3 sm:pl-14">
            <PriorWorkSummary projectId={projectId} epic={epic} stories={stories} />
          </div>
          {stories.length ? (
            <div className="divide-border/70 flex flex-col divide-y">
              {stories.map((story) => (
                <StoryTableRow
                  key={story.jira_key}
                  projectId={projectId}
                  story={story}
                  statusByKey={statusByKey}
                  autoRunEnabled={autoRunEnabled}
                  onOpen={onOpenStory}
                />
              ))}
            </div>
          ) : (
            <p className="text-muted-foreground px-4 py-4 text-sm sm:pl-14">
              No stories yet — they appear here once the ticket has some in {sourceLabel(epic.external_source)}.
            </p>
          )}
        </div>
      ) : null}

      <RemoveEpicDialog projectId={projectId} epic={epic} open={removing} onOpenChange={setRemoving} />
    </div>
  );
}
