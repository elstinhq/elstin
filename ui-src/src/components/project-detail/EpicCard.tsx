import { Pause, Play, RefreshCw } from "lucide-react";
import { PhaseTagFor, Tag } from "../shared/StatusTag";
import { Twisty } from "../shared/Twisty";
import { PrApprovalPrompt } from "./PrApprovalPrompt";
import { RemoveEpicButton } from "./RemoveEpicButton";
import { StoryRow } from "./StoryRow";
import { useRefreshPriorWork, useSetEpicGate } from "../../hooks/useEpics";
import { continuedBranches, doneWorkOutcome, lookupDue, needsChoice, onlyInDefault } from "../../lib/priorWork";
import { useToast } from "../../lib/toast";
import type { Epic, ProjectRepo, Story } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Progress } from "../ui/progress";
import { cn } from "../../lib/utils";
import { displayKey } from "../../lib/ticketSource";

// Wave first, then issue key — the order the epic actually executes in. The
// API returns rows in arbitrary DB order, which put the QA ticket at the
// top of PROJ-3480 even though it runs last.
const byWaveThenKey = (a: Story, b: Story) =>
  (a.wave ?? 0) - (b.wave ?? 0) || a.jira_key.localeCompare(b.jira_key, undefined, { numeric: true });

interface EpicCardProps {
  projectId: number;
  epic: Epic;
  repos: ProjectRepo[];
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  autoRunEnabled: boolean;
  expandedStories: Set<string>;
  onToggleExpandStory: (key: string) => void;
  onOpenEpic: (key: string) => void;
  onOpenStory: (key: string) => void;
}

export function EpicCard({
  projectId,
  epic,
  repos,
  isCollapsed,
  onToggleCollapse,
  autoRunEnabled,
  expandedStories,
  onToggleExpandStory,
  onOpenEpic,
  onOpenStory,
}: EpicCardProps) {
  const setEpicGate = useSetEpicGate(projectId);

  // Progress counts top-level stories only. Sub-tasks are never dispatched
  // and never reach a done status, so counting them would peg every epic's
  // progress bar well below the truth — PROJ-3480 would read 0/31 with all
  // five stories merged.
  const allRows = epic.stories || [];
  const stories = allRows.filter((s) => !s.parent_key).sort(byWaveThenKey);
  const subtasksByParent = new Map<string, Story[]>();
  for (const row of allRows) {
    if (!row.parent_key) continue;
    if (!subtasksByParent.has(row.parent_key)) subtasksByParent.set(row.parent_key, []);
    subtasksByParent.get(row.parent_key)!.push(row);
  }

  const statusByKey = new Map(stories.map((s) => [s.jira_key, s.status as string]));
  const doneCount = stories.filter((s) => s.status === "merged" || s.status === "done").length;
  // The epic's one pull request (per repo it wrote), opened once every story
  // has merged into the epic branch.
  const pullRequests = (epic.pull_requests || []).filter((pr) => pr.number || pr.error);
  const total = stories.length;
  const pct = total ? Math.round((doneCount / total) * 100) : 0;
  // Pause/Resume is a hold on dispatch, and the daemon stops polling an epic
  // once it is merged or complete (forge/state/manager_epic.py's
  // _TERMINAL_EPIC_PHASES), so on a finished epic the flag does nothing — yet
  // it was left set from the epic's creation (epics start paused) and read
  // "Paused / Resume" on work that was long done. Keyed on the phase rather
  // than a guess at the stories, so the control comes back by itself if the
  // backend ever reopens a finished epic.
  const finished = epic.phase === "merged" || epic.phase === "complete";

  return (
    <Card className="p-4">
      <div className="flex flex-col gap-4">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <Twisty isOpen={!isCollapsed} onToggle={onToggleCollapse} />
          <span
            className="cursor-pointer font-mono text-sm font-bold hover:underline"
            onClick={(e) => {
              e.stopPropagation();
              onOpenEpic(epic.jira_key);
            }}
          >
            {displayKey(epic)}
          </span>
          <PhaseTagFor phase={epic.phase} />

          {epic.repo ? (
            <span
              className="text-muted-foreground min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-xs"
              title={
                repos.length > 1
                  ? `Default repo for unprefixed paths. Every repo in this project (${repos
                      .map((r) => r.label || r.path)
                      .join(", ")}) is already readable and writable by the plan — assigned automatically, nothing to pick per ticket.`
                  : undefined
              }
            >
              {epic.repo}
              {repos.length > 1 ? " (default)" : ""}
            </span>
          ) : repos.length === 0 ? (
            <Tag
              label="No folder attached"
              appearance="warning"
              title="Add a git repo in Folders before this epic can run."
            />
          ) : null}

          {pullRequests.map((pr) =>
            pr.number ? (
              <Button key={pr.repo_label || "default"} variant="outline" size="sm" className="shrink-0" asChild>
                <a
                  href={pr.url || "#"}
                  target="_blank"
                  rel="noreferrer"
                  title={pr.ci_status ? `CI: ${pr.ci_status}` : "The epic's pull request"}
                >
                  {pr.repo_label ? `${pr.repo_label} ` : ""}#{pr.number} ({pr.state}
                  {pr.state === "open" && pr.ci_status ? `, CI ${pr.ci_status}` : ""})
                </a>
              </Button>
            ) : (
              <Tag key={pr.repo_label || "default"} label="PR not opened" appearance="destructive" title={pr.error || undefined} />
            ),
          )}

          {epic.manual_gate && !finished ? <Tag label="Paused" appearance="warning" /> : null}
          {epic.pr_approval === "awaiting" ? (
            <Tag label="Awaiting PR approval" appearance="warning" title="Every story has merged; nothing is pushed until you open the pull request or keep the branch local." />
          ) : null}
          {epic.external_closed ? (
            <Tag label="closed in source" appearance="secondary" title="The source reports this epic closed, so forge put it on hold. Resume it to keep going." />
          ) : null}

          <div className="flex-1" />

          {finished ? null : (
            <Button
              variant="outline"
              size="sm"
              loading={setEpicGate.isPending}
              onClick={(e) => {
                e.stopPropagation();
                setEpicGate.mutate({ epicKey: epic.jira_key, manualGate: !epic.manual_gate });
              }}
            >
              {epic.manual_gate ? <Play /> : <Pause />}
              {epic.manual_gate ? "Resume" : "Pause"}
            </Button>
          )}
          <RemoveEpicButton projectId={projectId} epic={epic} />
        </div>

        <div className={cn("flex flex-col gap-4", isCollapsed && "hidden")}>
          <PrApprovalPrompt projectId={projectId} epic={epic} />
          <div className="flex items-center gap-3">
            <Progress value={pct} className="flex-1" indicatorClassName="bg-success" />
            <span className="text-muted-foreground text-xs whitespace-nowrap">
              {doneCount}/{total} done
            </span>
          </div>

          <PriorWorkSummary projectId={projectId} epic={epic} stories={stories} />

          <div className="flex flex-col">
            {stories.map((story) => (
              <StoryRow
                key={story.jira_key}
                projectId={projectId}
                story={story}
                subtasks={(subtasksByParent.get(story.jira_key) || []).sort(byWaveThenKey)}
                statusByKey={statusByKey}
                autoRunEnabled={autoRunEnabled}
                expandedStories={expandedStories}
                onToggleExpand={onToggleExpandStory}
                onOpenStory={onOpenStory}
              />
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}

// What the look-up of earlier work found (forge/services/prior_work.py), in
// one line: which tickets continue an existing branch, which wait for a pick,
// which are already in main — and "Look again" while any ticket has not
// started, for a branch pushed after the epic was added.
function PriorWorkSummary({ projectId, epic, stories }: { projectId: number; epic: Epic; stories: Story[] }) {
  const refresh = useRefreshPriorWork(projectId);
  const { showToast } = useToast();
  const unstarted = stories.filter((s) => s.status === "pending" && !s.branch);
  const lookups = [epic.prior_work, ...stories.map((s) => s.prior_work)].filter((p) => p !== null);
  // Nothing is looked up until the project has a folder to look in.
  if (!epic.repo || (!lookups.length && !unstarted.length)) return null;

  const continuing = stories
    .filter((s) => s.status !== "skipped" && continuedBranches(s.prior_work).length)
    .map((s) => displayKey(s));
  const doneMerged = stories.filter((s) => s.status === "skipped" && doneWorkOutcome(s.prior_work) === "merged").map((s) => displayKey(s));
  const doneConflicts = stories.filter((s) => s.status === "skipped" && doneWorkOutcome(s.prior_work) === "conflict").map((s) => displayKey(s));
  const choosing = stories
    .filter((s) => (s.status === "pending" || s.status === "skipped") && !s.branch && needsChoice(s.prior_work).length)
    .map((s) => displayKey(s));
  const inMain = stories.filter((s) => onlyInDefault(s.prior_work)).map((s) => displayKey(s));
  const epicBranch = continuedBranches(epic.prior_work)[0]?.candidate.branch;
  const pendingLookup = unstarted.some((s) => lookupDue(s.prior_work));

  const parts = [
    epicBranch ? `epic branch continues ${epicBranch}` : null,
    continuing.length ? `continuing existing work: ${continuing.join(", ")}` : null,
    doneMerged.length ? `done tickets' work merged in: ${doneMerged.join(", ")}` : null,
    doneConflicts.length ? `done work that conflicts: ${doneConflicts.join(", ")}` : null,
    choosing.length ? `pick a branch: ${choosing.join(", ")}` : null,
    inMain.length ? `already in main: ${inMain.join(", ")}` : null,
  ].filter(Boolean);
  const text = pendingLookup
    ? "Looking for earlier work on these tickets…"
    : parts.length
      ? `Earlier work — ${parts.join(" · ")}`
      : "No earlier work found for these tickets";

  return (
    <div className="text-muted-foreground flex min-w-0 items-center gap-2 text-xs">
      <span className="min-w-0 truncate" title={text}>{text}</span>
      {unstarted.length && !pendingLookup ? (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 shrink-0 px-2 text-xs"
          loading={refresh.isPending}
          title="Look for branches again — e.g. one pushed after this epic was added. Tickets already started keep the branch they started from."
          onClick={(e) => {
            e.stopPropagation();
            refresh.mutate(epic.jira_key, {
              onSuccess: () => showToast("Looking again on the next poll", "success"),
              onError: (err) => showToast(err.message || "Could not look again", "error"),
            });
          }}
        >
          <RefreshCw className="size-3" />
          Look again
        </Button>
      ) : null}
    </div>
  );
}
