import { useEffect, type ReactNode } from "react";
import { useProject } from "../hooks/useProjects";
import { matchPath, navigate, useLocation } from "../lib/router";
import type { Project } from "../types/api";
import { PoSessionPage } from "./project-detail/po/PoSessionPage";
import { PoStartPage } from "./project-detail/po/PoStartPage";
import { SettingsPage } from "./settings/SettingsPage";
import { AppLayout } from "./shell/AppLayout";
import { Page } from "./shell/Page";
import { TasksView } from "./tasks/TasksView";
import { HomeView } from "./views/HomeView";
import { BacklogView, FoldersView, LessonsView, NotFoundView, ProjectSettingsView } from "./views/ProjectViews";
import { Skeleton } from "./ui/skeleton";

// A route that has moved: replace the URL (so Back does not bounce) and render
// nothing meanwhile.
function Redirect({ to }: { to: string }) {
  useEffect(() => navigate(to, { replace: true }), [to]);
  return null;
}

// Every project view needs its project; this loads it once and says so when
// there is no such project, rather than each view doing both.
function WithProject({ id, children }: { id: string; children: (project: Project) => ReactNode }) {
  const projectId = Number(id);
  const query = useProject(Number.isInteger(projectId) && projectId > 0 ? projectId : -1);
  if (!Number.isInteger(projectId) || projectId <= 0 || (query.isSuccess && !query.data) || query.isError) {
    return <NotFoundView what="project" />;
  }
  if (!query.data) {
    return (
      <Page crumbs={[{ label: "Loading…" }]}>
        <Skeleton className="h-40 rounded-xl" />
      </Page>
    );
  }
  return <>{children(query.data)}</>;
}

type Route = [pattern: string, render: (params: Record<string, string>) => ReactNode];

// The app's URLs. elstin/app.py serves the same shell for every one of them,
// so a new view needs an entry here and nothing on the server.
const ROUTES: Route[] = [
  ["/", () => <HomeView />],
  ["/projects", () => <HomeView />],
  ["/settings", () => <SettingsPage />],
  ["/projects/:id", ({ id }) => <Redirect to={`/projects/${id}/tasks${window.location.search}`} />],
  ["/projects/:id/tasks", ({ id }) => <WithProject id={id!}>{(project) => <TasksView project={project} />}</WithProject>],
  ["/projects/:id/folders", ({ id }) => <WithProject id={id!}>{(project) => <FoldersView project={project} />}</WithProject>],
  ["/projects/:id/lessons", ({ id }) => <WithProject id={id!}>{(project) => <LessonsView project={project} />}</WithProject>],
  ["/projects/:id/backlog", ({ id }) => <WithProject id={id!}>{(project) => <BacklogView project={project} />}</WithProject>],
  ["/projects/:id/backlog/new", ({ id }) => <PoStartPage projectId={Number(id)} />],
  ["/projects/:id/backlog/:sid", ({ id, sid }) => <PoSessionPage projectId={Number(id)} sessionId={Number(sid)} />],
  ["/projects/:id/settings", ({ id }) => <WithProject id={id!}>{(project) => <ProjectSettingsView project={project} />}</WithProject>],
  // The PO pages' URLs before the app had one shell.
  ["/projects/:id/po/new", ({ id }) => <Redirect to={`/projects/${id}/backlog/new`} />],
  ["/projects/:id/po/:sid", ({ id, sid }) => <Redirect to={`/projects/${id}/backlog/${sid}`} />],
];

export function App() {
  const { pathname } = useLocation();
  for (const [pattern, render] of ROUTES) {
    const params = matchPath(pattern, pathname);
    // Keyed by the matched pattern and its params, so moving between two
    // projects (or two drafts) remounts the view instead of carrying one's
    // local state into the other.
    if (params) return <AppLayout key="layout">{<div key={`${pattern}:${JSON.stringify(params)}`} className="contents">{render(params)}</div>}</AppLayout>;
  }
  return (
    <AppLayout key="layout">
      <NotFoundView />
    </AppLayout>
  );
}
