import { useState } from "react";
import { ClipboardList } from "lucide-react";
import { EmptyState } from "../shared/EmptyState";
import { EpicCard } from "./EpicCard";
import type { Epic, ProjectRepo } from "../../types/api";

interface EpicListProps {
  projectId: number;
  epics: Epic[];
  repos: ProjectRepo[];
  autoRunEnabled: boolean;
  onOpenEpic: (key: string) => void;
  onOpenStory: (key: string) => void;
}

// Expanded/collapsed state is keyed by JIRA key and lives here rather than
// on the epic/story objects themselves, because the epics query refetches
// (and returns new object identities) every 5s — a card the user expanded
// must not snap shut just because a poll landed.
export function EpicList({ projectId, epics, repos, autoRunEnabled, onOpenEpic, onOpenStory }: EpicListProps) {
  const [collapsedEpics, setCollapsedEpics] = useState<Set<string>>(new Set());
  const [expandedStories, setExpandedStories] = useState<Set<string>>(new Set());

  function toggleCollapse(key: string) {
    setCollapsedEpics((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleExpandStory(key: string) {
    setExpandedStories((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (!epics.length) {
    return (
      <EmptyState
        icon={ClipboardList}
        heading="No tasks yet"
        body='Use "Add ticket" above to sync one from JIRA. New tickets start paused until you resume.'
      />
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {epics.map((epic) => (
        <EpicCard
          key={epic.jira_key}
          projectId={projectId}
          epic={epic}
          repos={repos}
          isCollapsed={collapsedEpics.has(epic.jira_key)}
          onToggleCollapse={() => toggleCollapse(epic.jira_key)}
          autoRunEnabled={autoRunEnabled}
          expandedStories={expandedStories}
          onToggleExpandStory={toggleExpandStory}
          onOpenEpic={onOpenEpic}
          onOpenStory={onOpenStory}
        />
      ))}
    </div>
  );
}
