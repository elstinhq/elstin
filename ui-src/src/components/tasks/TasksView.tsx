import { useMemo, useState } from "react";
import { Filter, Plus, Sparkles } from "lucide-react";
import { useProjectEpics } from "../../hooks/useEpics";
import { useProjectRepos } from "../../hooks/useFolders";
import { useInbox } from "../../hooks/useInbox";
import { useProjectConfig } from "../../hooks/useProjectConfig";
import { useTicketFilter } from "../../hooks/useTicketFilter";
import { Link, navigate, useLocation, withSearch } from "../../lib/router";
import { storyGroup } from "../../lib/status";
import type { Project } from "../../types/api";
import { DetailDrawer, type DetailTarget } from "../detail/DetailDrawer";
import { NeedsYouCards } from "../inbox/NeedsYou";
import { SyncTicketDialog } from "../project-detail/SyncTicketDialog";
import { TicketFilterDialog } from "../project-detail/TicketFilterDialog";
import { Page, SectionHeading } from "../shell/Page";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { EpicTable } from "./EpicTable";
import { StatusLegend } from "./StoryProgress";

// The project's home: what is waiting on a person, then every ticket on the
// board with its stories. The drawer for one of them lives in the URL
// (?story=KEY or ?epic=KEY, and &tab=), so it can be linked to and Back
// closes it.
export function TasksView({ project }: { project: Project }) {
  const projectId = project.id;
  const epicsQuery = useProjectEpics(projectId);
  const reposQuery = useProjectRepos(projectId);
  const configQuery = useProjectConfig(projectId);
  const filterQuery = useTicketFilter(projectId);
  const inbox = useInbox();
  const { search } = useLocation();
  const [syncOpen, setSyncOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);

  const epics = epicsQuery.data ?? [];
  const repos = reposQuery.data ?? [];
  const items = (inbox.data?.items ?? []).filter((item) => item.project_id === projectId);
  const ticketSource = project.ticket_source ?? "jira";

  // Resolved (project override, else global default) — the defaults keep the
  // page from flashing the wrong state before settings have loaded once.
  const jiraBaseUrl = useMemo(() => configQuery.data?.find((f) => f.key === "jira_base_url")?.value ?? null, [configQuery.data]);
  const autoRunEnabled = useMemo(() => {
    const field = configQuery.data?.find((f) => f.key === "auto_run_enabled");
    return field ? String(field.value).toLowerCase() === "true" : true;
  }, [configQuery.data]);
  const anyFailed = epics.some((e) => (e.stories ?? []).some((s) => !s.parent_key && storyGroup(s) === "failed"));

  const storyKey = search.get("story");
  const epicKey = search.get("epic");
  const target: DetailTarget | null = storyKey ? { kind: "story", key: storyKey } : epicKey ? { kind: "epic", key: epicKey } : null;

  const openStory = (key: string) => navigate(withSearch({ story: key, epic: null, tab: null }));
  const openEpic = (key: string, tab?: string) => navigate(withSearch({ epic: key, story: null, tab: tab ?? null }));

  const filter = filterQuery.data;
  // One line of state, not a panel: the filter's own dialog carries the
  // detail, and this only has to answer "is anything pulling tickets in?".
  const filterStatus = filter ? (
    filter.enabled ? (
      <span className="text-muted-foreground hidden items-center gap-2 text-sm lg:inline-flex">
        <span className="bg-success size-2 rounded-full" aria-hidden="true" />
        Auto-adding {filter.jira_project_key || "matching"} tickets
      </span>
    ) : (
      <span className="text-muted-foreground hidden text-sm lg:inline">Filter saved, sync off</span>
    )
  ) : null;

  return (
    <Page
      crumbs={[{ label: project.name, href: `/projects/${projectId}/tasks` }, { label: "Tasks" }]}
      title={`${project.name} · Tasks`}
      actions={
        <>
          {filterStatus}
          <Button variant="outline" onClick={() => setFilterOpen(true)}>
            <Filter />
            Filter
          </Button>
          <Button variant="outline" asChild title="Have the PO agent draft epics, stories and tasks">
            <Link href={`/projects/${projectId}/backlog/new`}>
              <Sparkles />
              Plan backlog
            </Link>
          </Button>
          <Button onClick={() => setSyncOpen(true)}>
            <Plus />
            Add ticket
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-10">
        <section className="flex flex-col gap-4">
          <SectionHeading title="Needs you" note="Nothing here moves until someone acts" />
          {inbox.isPending ? <Skeleton className="h-36 rounded-xl" /> : <NeedsYouCards items={items} projectId={projectId} />}
        </section>

        <section className="flex flex-col gap-4">
          <SectionHeading title="Tasks" note="Refreshes every 5s">
            <StatusLegend showFailed={anyFailed} />
          </SectionHeading>
          {epicsQuery.isPending ? (
            <div className="flex flex-col gap-2">
              <Skeleton className="h-14 rounded-xl" />
              <Skeleton className="h-14 rounded-xl" />
            </div>
          ) : epicsQuery.isError ? (
            <p className="text-destructive text-sm">Could not load the tasks: {epicsQuery.error.message}</p>
          ) : (
            <EpicTable
              projectId={projectId}
              epics={epics}
              repos={repos}
              autoRunEnabled={autoRunEnabled}
              onOpenEpic={openEpic}
              onOpenStory={openStory}
            />
          )}
        </section>
      </div>

      <SyncTicketDialog projectId={projectId} ticketSource={ticketSource} open={syncOpen} onClose={() => setSyncOpen(false)} />
      <TicketFilterDialog projectId={projectId} ticketSource={ticketSource} open={filterOpen} onClose={() => setFilterOpen(false)} />
      <DetailDrawer
        projectId={projectId}
        epics={epics}
        target={target}
        tab={search.get("tab")}
        onTabChange={(tab) => navigate(withSearch({ tab }), { replace: true })}
        onClose={() => navigate(withSearch({ story: null, epic: null, tab: null }))}
        onOpenStory={openStory}
        jiraBaseUrl={jiraBaseUrl}
        ticketSource={ticketSource}
      />
    </Page>
  );
}
