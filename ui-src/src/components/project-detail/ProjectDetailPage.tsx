import { useMemo, useState } from "react";
import { Cog, Filter, Plus, Sparkles } from "lucide-react";
import { AppShell } from "../shared/AppShell";
import { FoldersPanel } from "./FoldersPanel";
import { LessonsPanel } from "./LessonsPanel";
import { PoSessionsPanel } from "./po/PoSessionsPanel";
import { EpicList } from "./EpicList";
import { DetailDialog, type DetailTarget } from "./DetailDialog";
import { SyncTicketDialog } from "./SyncTicketDialog";
import { TicketFilterDialog } from "./TicketFilterDialog";
import { ProjectSettingsDialog } from "./ProjectSettingsDialog";
import { useProject } from "../../hooks/useProjects";
import { useProjectEpics } from "../../hooks/useEpics";
import { useProjectRepos } from "../../hooks/useFolders";
import { useProjectConfig } from "../../hooks/useProjectConfig";
import { useTicketFilter } from "../../hooks/useTicketFilter";
import { Button } from "../ui/button";

function StatChip({ value, label, dot = false }: { value: number; label: string; dot?: boolean }) {
  return (
    <div className="border-border flex items-center gap-2 rounded-lg border px-3.5 py-2">
      {dot ? <span className="bg-warning size-2 shrink-0 rounded-full" /> : null}
      <span className="text-sm font-bold">{value}</span>
      <span className="text-muted-foreground text-sm">{label}</span>
    </div>
  );
}

const GATED_STATUSES = new Set(["awaiting_plan_approval"]);

export function ProjectDetailPage({ projectId }: { projectId: number }) {
  const projectQuery = useProject(projectId);
  const epicsQuery = useProjectEpics(projectId);
  const reposQuery = useProjectRepos(projectId);
  const configQuery = useProjectConfig(projectId);

  const ticketFilterQuery = useTicketFilter(projectId);

  const [syncDialogOpen, setSyncDialogOpen] = useState(false);
  const [filterDialogOpen, setFilterDialogOpen] = useState(false);
  const [settingsDialogOpen, setSettingsDialogOpen] = useState(false);
  const [detailTarget, setDetailTarget] = useState<DetailTarget | null>(null);

  const ticketSource = projectQuery.data?.ticket_source ?? "jira";
  const epics = epicsQuery.data ?? [];
  const repos = reposQuery.data ?? [];

  // Resolved (project override, else global default) — defaults keep the
  // page from flashing the wrong state before settings have loaded once.
  const jiraBaseUrl = useMemo(() => configQuery.data?.find((f) => f.key === "jira_base_url")?.value ?? null, [configQuery.data]);
  const autoRunEnabled = useMemo(() => {
    const field = configQuery.data?.find((f) => f.key === "auto_run_enabled");
    return field ? String(field.value).toLowerCase() === "true" : true;
  }, [configQuery.data]);

  const gatedCount = useMemo(
    () => epics.reduce((count, epic) => count + (epic.stories || []).filter((s) => GATED_STATUSES.has(s.status)).length, 0),
    [epics],
  );

  if (projectQuery.isSuccess && !projectQuery.data) {
    return (
      <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
        <h1 className="text-xl font-semibold">Project not found</h1>
      </AppShell>
    );
  }

  return (
    <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
      <div className="flex flex-col gap-6">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold">{projectQuery.data?.name ?? ""}</h1>
          <p className="text-muted-foreground text-sm">{projectQuery.data?.description ?? ""}</p>
        </div>

        <div className="flex flex-wrap gap-3">
          <StatChip value={repos.length} label="folders attached" />
          <StatChip value={epics.length} label={epics.length === 1 ? "active task" : "active tasks"} />
          <StatChip value={gatedCount} label="waiting on approval" dot />
        </div>

        <div className="grid grid-cols-1 items-start gap-6 lg:grid-cols-[2fr_1fr]">
          <section className="flex min-w-0 flex-col gap-4">
            <div className="flex items-center gap-3">
              <h2 className="text-base font-semibold">Tasks</h2>
              <span className="text-muted-foreground text-xs">Refreshes every 5s</span>
            </div>
            <p className="text-muted-foreground text-xs">
              Approving a story's plan starts its development — the next daemon poll cycle (up to 15s) picks it up. Finished stories merge into the epic branch on their own; the epic opens one pull request once all have.
            </p>
            <EpicList
              projectId={projectId}
              epics={epics}
              repos={repos}
              autoRunEnabled={autoRunEnabled}
              onOpenEpic={(key) => setDetailTarget({ kind: "epic", key })}
              onOpenStory={(key) => setDetailTarget({ kind: "story", key })}
            />
          </section>

          <div className="flex min-w-0 flex-col gap-6">
            <div className="flex flex-col gap-2">
              <Button onClick={() => setSyncDialogOpen(true)}>
                <Plus />
                Add ticket
              </Button>
              <Button variant="outline" asChild title="Have the PO agent draft epics, stories and tasks">
                <a href={`/projects/${projectId}/po/new`}>
                  <Sparkles />
                  Plan backlog
                </a>
              </Button>
              <Button variant="outline" onClick={() => setFilterDialogOpen(true)}>
                <Filter />
                Ticket filter
              </Button>
              {/* One line of state, not a panel: the filter's own dialog
                  carries the detail, and this only has to answer "is
                  anything pulling tickets in on its own?". */}
              <span className="text-muted-foreground text-xs">
                {ticketFilterQuery.data
                  ? ticketFilterQuery.data.enabled
                    ? `Auto-adding tickets matching ${ticketFilterQuery.data.jira_project_key || "this filter"}`
                    : "Filter saved, background sync off"
                  : "No filter — tickets are added by hand"}
              </span>
            </div>

            <FoldersPanel projectId={projectId} />

            <LessonsPanel projectId={projectId} />

            <PoSessionsPanel projectId={projectId} />

            <Button variant="outline" onClick={() => setSettingsDialogOpen(true)}>
              <Cog />
              Settings
            </Button>
          </div>
        </div>
      </div>

      <SyncTicketDialog projectId={projectId} ticketSource={ticketSource} open={syncDialogOpen} onClose={() => setSyncDialogOpen(false)} />
      <TicketFilterDialog projectId={projectId} ticketSource={ticketSource} open={filterDialogOpen} onClose={() => setFilterDialogOpen(false)} />
      <ProjectSettingsDialog projectId={projectId} ticketSource={ticketSource} open={settingsDialogOpen} onClose={() => setSettingsDialogOpen(false)} />
      <DetailDialog
        projectId={projectId}
        epics={epics}
        target={detailTarget}
        onClose={() => setDetailTarget(null)}
        jiraBaseUrl={jiraBaseUrl}
        ticketSource={ticketSource}
      />
    </AppShell>
  );
}
