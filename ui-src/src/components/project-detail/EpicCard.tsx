import { Pause, Play } from "lucide-react";
import { PhaseTagFor, Tag } from "../shared/StatusTag";
import { Twisty } from "../shared/Twisty";
import { StoryRow } from "./StoryRow";
import { useSetEpicGate, useSetEpicRepo } from "../../hooks/useEpics";
import type { Epic, ProjectRepo, Story } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Progress } from "../ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { cn } from "../../lib/utils";

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
  const setEpicRepo = useSetEpicRepo(projectId);
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

  const doneCount = stories.filter((s) => s.status === "merged" || s.status === "done").length;
  const total = stories.length;
  const pct = total ? Math.round((doneCount / total) * 100) : 0;

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
            {epic.jira_key}
          </span>
          <PhaseTagFor phase={epic.phase} />

          {epic.repo ? (
            <span className="text-muted-foreground min-w-0 overflow-hidden text-ellipsis whitespace-nowrap text-xs">
              {epic.repo}
            </span>
          ) : repos.length ? (
            <span onClick={(event) => event.stopPropagation()}>
              <Select onValueChange={(value) => setEpicRepo.mutate({ epicKey: epic.jira_key, projectRepoId: Number(value) })}>
                <SelectTrigger size="sm" aria-label="Folder">
                  <SelectValue placeholder="Choose a folder" />
                </SelectTrigger>
                <SelectContent>
                  {repos.map((r) => (
                    <SelectItem key={r.id} value={String(r.id)}>
                      {r.label || r.path}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </span>
          ) : (
            <Tag label="No folder attached" appearance="warning" />
          )}

          {epic.manual_gate ? <Tag label="Paused" appearance="warning" /> : null}

          <div className="flex-1" />

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
        </div>

        <div className={cn("flex flex-col gap-4", isCollapsed && "hidden")}>
          <div className="flex items-center gap-3">
            <Progress value={pct} className="flex-1" indicatorClassName="bg-success" />
            <span className="text-muted-foreground text-xs whitespace-nowrap">
              {doneCount}/{total} done
            </span>
          </div>

          <div className="flex flex-col">
            {stories.map((story) => (
              <StoryRow
                key={story.jira_key}
                projectId={projectId}
                story={story}
                subtasks={(subtasksByParent.get(story.jira_key) || []).sort(byWaveThenKey)}
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
