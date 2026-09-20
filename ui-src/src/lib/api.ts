import type {
  ConfigField,
  Epic,
  EpicUsage,
  ExternalChangeAction,
  Project,
  ProjectRepo,
  RepoGuide,
  Story,
  StoryUsage,
  TicketFilter,
  TicketFilterInput,
  TicketFilterPreview,
  TicketFilterSyncResult,
} from "../types/api";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json", ...init.headers } : init?.headers,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.detail || `Request failed (${res.status})`, res.status);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
const put = (body: unknown): RequestInit => ({ method: "PUT", body: JSON.stringify(body) });

export const api = {
  // -- projects --------------------------------------------------------
  listProjects: () => request<Project[]>("/api/projects"),
  createProject: (body: { name: string; description: string; repo_url: string | null }) =>
    request<Project>("/api/projects", json(body)),
  getProject: (id: number) => request<Project>(`/api/projects/${id}`),
  updateProject: (id: number, body: { ticket_source?: string; name?: string; description?: string }) =>
    request<Project>(`/api/projects/${id}`, put(body)),

  // -- folders (project repos) -----------------------------------------
  listProjectRepos: (projectId: number) => request<ProjectRepo[]>(`/api/projects/${projectId}/repos`),
  cloneProjectRepo: (projectId: number, body: { url: string; label?: string | null }) =>
    request<ProjectRepo>(`/api/projects/${projectId}/repos`, json(body)),
  removeProjectRepo: (projectId: number, repoId: number) =>
    request<{ repo_id: number; removed: boolean }>(`/api/projects/${projectId}/repos/${repoId}`, {
      method: "DELETE",
    }),

  getRepoGuide: (projectId: number, repoId: number) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide`),
  generateRepoGuide: (projectId: number, repoId: number) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide/generate`, { method: "POST" }),
  // Empty content discards the generated guide.
  saveRepoGuide: (projectId: number, repoId: number, content: string) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide`, put({ content })),

  // -- project-scoped config --------------------------------------------
  getProjectConfig: (projectId: number) => request<ConfigField[]>(`/api/projects/${projectId}/config`),
  setProjectConfig: (projectId: number, key: string, value: string) =>
    request<{ key: string; updated: boolean }>(`/api/projects/${projectId}/config/${encodeURIComponent(key)}`, put({ value })),
  clearProjectConfig: (projectId: number, key: string) =>
    request<{ key: string; cleared: boolean }>(`/api/projects/${projectId}/config/${encodeURIComponent(key)}`, {
      method: "DELETE",
    }),

  // -- ticket filter -------------------------------------------------------
  // preview/sync take an optional unsaved filter so the dialog can test an
  // edit before committing it; omitting it runs whatever is saved.
  getTicketFilter: (projectId: number) =>
    request<{ filter: TicketFilter | null }>(`/api/projects/${projectId}/ticket-filter`),
  setTicketFilter: (projectId: number, body: TicketFilterInput) =>
    request<{ filter: TicketFilter }>(`/api/projects/${projectId}/ticket-filter`, put(body)),
  clearTicketFilter: (projectId: number) =>
    request<{ project_id: number; cleared: boolean }>(`/api/projects/${projectId}/ticket-filter`, {
      method: "DELETE",
    }),
  previewTicketFilter: (projectId: number, filter?: TicketFilterInput) =>
    request<TicketFilterPreview>(`/api/projects/${projectId}/ticket-filter/preview`, json({ filter: filter ?? null })),
  runTicketFilterSync: (projectId: number, filter?: TicketFilterInput) =>
    request<TicketFilterSyncResult>(`/api/projects/${projectId}/ticket-filter/sync`, json({ filter: filter ?? null })),

  // -- global config -----------------------------------------------------
  getGlobalConfig: () => request<ConfigField[]>("/api/config"),
  setGlobalConfig: (key: string, value: string) =>
    request<{ key: string; updated: boolean }>(`/api/config/${encodeURIComponent(key)}`, put({ value })),

  // -- epics ---------------------------------------------------------------
  listProjectEpics: (projectId: number) => request<Epic[]>(`/api/projects/${projectId}/epics`),
  syncEpic: (jiraKey: string, projectId: number) =>
    request<Epic>("/api/epics/sync", json({ jira_key: jiraKey, project_id: projectId })),
  syncEpicFields: (epicKey: string) => request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/sync`, { method: "POST" }),
  setEpicRepo: (epicKey: string, projectRepoId: number) =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/repo`, put({ project_repo_id: projectRepoId })),
  setEpicGate: (epicKey: string, manualGate: boolean) =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/gate`, put({ manual_gate: manualGate })),
  getEpicUsage: (epicKey: string) => request<EpicUsage>(`/api/epics/${encodeURIComponent(epicKey)}/usage`),

  // -- stories -------------------------------------------------------------
  getStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}`),
  startStory: (storyKey: string) =>
    request<{ story_key: string; triggered: string }>(`/api/stories/${encodeURIComponent(storyKey)}/start`, {
      method: "POST",
    }),
  approveStory: (storyKey: string) =>
    request<{ story_key: string; gate_approved: boolean }>(`/api/stories/${encodeURIComponent(storyKey)}/approve`, {
      method: "POST",
    }),
  retryStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/retry`, { method: "POST" }),
  setStoryKind: (storyKey: string, kind: string) =>
    request<{ story_key: string; kind: string; epic_key: string }>(
      `/api/stories/${encodeURIComponent(storyKey)}/kind`,
      put({ kind }),
    ),
  resolveExternalChange: (storyKey: string, action: ExternalChangeAction) =>
    request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/external-change`, json({ action })),
  syncStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/sync`, { method: "POST" }),
  // Not a fetch: EventSource opens this itself. It lives here anyway so
  // every server URL the UI knows about is still in one file. (The plain
  // snapshot at /activity is still served — the UI just has no reason to
  // ask for one when the stream opens with the same history.)
  storyActivityStreamUrl: (storyKey: string) => `/api/stories/${encodeURIComponent(storyKey)}/stream`,
  getStoryUsage: (storyKey: string) => request<StoryUsage>(`/api/stories/${encodeURIComponent(storyKey)}/usage`),
};
