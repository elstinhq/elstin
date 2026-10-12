import { useState } from "react";
import { ClipboardList } from "lucide-react";
import type { Epic, ProjectRepo } from "../../types/api";
import { EmptyState } from "../shared/EmptyState";
import { EpicRow } from "./EpicRow";

interface EpicTableProps {
  projectId: number;
  epics: Epic[];
  repos: ProjectRepo[];
  autoRunEnabled: boolean;
  onOpenEpic: (key: string, tab?: string) => void;
  onOpenStory: (key: string) => void;
}

const finished = (epic: Epic) => epic.phase === "merged" || epic.phase === "complete";

// Work still under way first, then what is finished; newest key first within
// each. The API returns epics in database order, which put a long-merged epic
// above the one being worked on.
const byActivityThenKey = (a: Epic, b: Epic) =>
  Number(finished(a)) - Number(finished(b)) || b.jira_key.localeCompare(a.jira_key, undefined, { numeric: true });

// Expanded/collapsed state is keyed by ticket key and lives here rather than
// on the epic objects themselves, because the epics query refetches (and
// returns new object identities) every 5s — a row the person opened must not
// snap shut because a poll landed. Until someone toggles one, the first epic
// still in progress starts open.
export function EpicTable({ projectId, epics, repos, autoRunEnabled, onOpenEpic, onOpenStory }: EpicTableProps) {
  const [toggled, setToggled] = useState<Map<string, boolean>>(new Map());
  const sorted = [...epics].sort(byActivityThenKey);
  const firstActive = sorted.find((e) => !finished(e))?.jira_key;

  if (!epics.length) {
    return (
      <EmptyState
        icon={ClipboardList}
        heading="No tasks yet"
        body='Use "Add ticket" to sync one from your ticket source, or "Plan backlog" to have the PO agent draft one. New tickets start paused until you resume them.'
      />
    );
  }

  return (
    <div className="bg-card overflow-hidden rounded-xl border shadow-xs">
      {sorted.map((epic) => {
        const expanded = toggled.get(epic.jira_key) ?? epic.jira_key === firstActive;
        return (
          <EpicRow
            key={epic.jira_key}
            projectId={projectId}
            epic={epic}
            repos={repos}
            expanded={expanded}
            onToggle={() => setToggled((prev) => new Map(prev).set(epic.jira_key, !expanded))}
            autoRunEnabled={autoRunEnabled}
            onOpenEpic={onOpenEpic}
            onOpenStory={onOpenStory}
          />
        );
      })}
    </div>
  );
}
