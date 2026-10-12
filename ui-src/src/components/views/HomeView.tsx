import { useState } from "react";
import { FolderOpen, Plus } from "lucide-react";
import { useInbox } from "../../hooks/useInbox";
import { useProjects } from "../../hooks/useProjects";
import { Link } from "../../lib/router";
import { NeedsYouCards } from "../inbox/NeedsYou";
import { CreateProjectModal } from "../projects/CreateProjectModal";
import { EmptyState } from "../shared/EmptyState";
import { Page, SectionHeading } from "../shell/Page";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";

// Where the app opens: everything waiting on a person across every project,
// grouped by project, then the projects themselves.
export function HomeView() {
  const projectsQuery = useProjects();
  const inbox = useInbox();
  const [creating, setCreating] = useState(false);
  const projects = projectsQuery.data ?? [];
  const items = inbox.data?.items ?? [];
  const needsByProject = new Map((inbox.data?.projects ?? []).map((p) => [p.id, p.counts.needs_you]));
  const waiting = projects.filter((p) => items.some((i) => i.project_id === p.id));

  return (
    <Page
      crumbs={[{ label: "Projects" }]}
      title="Projects"
      actions={
        <Button onClick={() => setCreating(true)}>
          <Plus />
          New project
        </Button>
      }
    >
      <div className="flex flex-col gap-10">
        <section className="flex flex-col gap-4">
          <SectionHeading title="Needs you" note="Across every project" />
          {inbox.isPending ? (
            <Skeleton className="h-36 rounded-xl" />
          ) : waiting.length === 0 ? (
            <NeedsYouCards items={[]} />
          ) : (
            waiting.map((project) => (
              <div key={project.id} className="flex flex-col gap-3">
                <Link href={`/projects/${project.id}/tasks`} className="text-muted-foreground hover:text-foreground w-fit text-sm font-medium">
                  {project.name}
                </Link>
                <NeedsYouCards items={items.filter((i) => i.project_id === project.id)} projectId={project.id} />
              </div>
            ))
          )}
        </section>

        <section className="flex flex-col gap-4">
          <SectionHeading title="Projects" />
          {projectsQuery.isPending ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
              <Skeleton className="h-28 rounded-xl" />
              <Skeleton className="h-28 rounded-xl" />
            </div>
          ) : projects.length === 0 ? (
            <EmptyState icon={FolderOpen} heading="No projects yet" body="Create one to get started: give it a name and the git repos it works on." />
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {projects.map((project) => {
                const needs = needsByProject.get(project.id) ?? 0;
                return (
                  <Link
                    key={project.id}
                    href={`/projects/${project.id}/tasks`}
                    className="bg-card hover:border-foreground/25 flex flex-col gap-2 rounded-xl border p-5 shadow-xs transition-colors"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-semibold">{project.name}</span>
                      {needs > 0 ? (
                        <span className="bg-warning text-warning-foreground inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-semibold">
                          {needs}
                        </span>
                      ) : null}
                    </div>
                    {project.description ? <p className="text-muted-foreground line-clamp-2 text-sm">{project.description}</p> : null}
                    <p className="text-muted-foreground mt-auto text-xs">
                      {project.repo_count} folder{project.repo_count === 1 ? "" : "s"} · {project.epic_count} task{project.epic_count === 1 ? "" : "s"}
                    </p>
                  </Link>
                );
              })}
            </div>
          )}
        </section>
      </div>
      <CreateProjectModal open={creating} onClose={() => setCreating(false)} />
    </Page>
  );
}
