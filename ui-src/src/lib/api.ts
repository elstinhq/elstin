import type {
  ConfigCategory,
  ConfigField,
  ModelOptions,
  Epic,
  EpicUsage,
  ExternalChangeAction,
  PoAnyDraft,
  PoModePreview,
  PoSession,
  PoSessionSummary,
  Project,
  ProjectLesson,
  ProjectRepo,
  LessonStatus,
  DeleteProjectReport,
  FolderListing,
  Inbox,
  ScanFolderResult,
  RemoveTicketReport,
  RepoGuide,
  Story,
  StoryPlan,
  StoryUsage,
  TicketFilter,
  TicketFilterInput,
  TicketFilterPreview,
  TicketFilterSyncResult,
} from "../types/api";

// One selection per pending ClarificationQuestion, submitted to
// POST /api/stories/{key}/answer.
export interface ClarificationAnswerInput {
  question_id: string;
  option_id: string;
}

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

// One answer to one of the PO agent's open questions: an option, or the
// person's own words (both is fine — the words qualify the option).
export interface PoAnswerInput {
  question_id: string;
  option_id?: string | null;
  text?: string | null;
}

export const api = {
  // -- projects --------------------------------------------------------
  listProjects: () => request<Project[]>("/api/projects"),
  createProject: (body: {
    name: string;
    description: string;
    repo_url?: string | null;
    repo_path?: string | null;
    repo_paths?: string[];
    read_only_paths?: string[];
    reference_path?: string | null;
  }) =>
    request<Project>("/api/projects", json(body)),
  getProject: (id: number) => request<Project>(`/api/projects/${id}`),
  updateProject: (id: number, body: { ticket_source?: string; name?: string; description?: string }) =>
    request<Project>(`/api/projects/${id}`, put(body)),
  // Read-only: a folder's subfolders, for the folder picker (home when no path).
  browseFolder: (path?: string | null) => request<FolderListing>("/api/projects/browse-folder", json({ path: path || null })),
  // Read-only: is this folder a git repo, or one holding repos to pick from?
  scanFolder: (path: string, projectId?: number) =>
    request<ScanFolderResult>("/api/projects/scan-folder", json({ path, project_id: projectId ?? null })),
  // The project and everything elstin holds for it; attached folders stay, GitHub/JIRA untouched.
  deleteProject: (id: number) => request<DeleteProjectReport>(`/api/projects/${id}`, { method: "DELETE" }),

  // -- folders (project repos) -----------------------------------------
  listProjectRepos: (projectId: number) => request<ProjectRepo[]>(`/api/projects/${projectId}/repos`),
  // Exactly one of url (clone it) or path (attach a repo already on disk).
  addProjectRepo: (
    projectId: number,
    body: { url?: string; path?: string; label?: string | null; kind?: "repo" | "reference"; read_only?: boolean },
  ) =>
    request<ProjectRepo>(`/api/projects/${projectId}/repos`, json(body)),
  // Mark a git repo read-only (or writable again); epics defaulting to it move to the next writable repo.
  setProjectRepoReadOnly: (projectId: number, repoId: number, readOnly: boolean) =>
    request<ProjectRepo & { epics_moved: string[]; moved_to: number | null }>(
      `/api/projects/${projectId}/repos/${repoId}`,
      put({ read_only: readOnly }),
    ),
  removeProjectRepo: (projectId: number, repoId: number) =>
    // Epics that defaulted to it move to the project's next folder (moved_to; null = none left).
    request<{ repo_id: number; removed: boolean; epics_moved: string[]; moved_to: number | null }>(`/api/projects/${projectId}/repos/${repoId}`, {
      method: "DELETE",
    }),

  getRepoGuide: (projectId: number, repoId: number) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide`),
  generateRepoGuide: (projectId: number, repoId: number) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide/generate`, { method: "POST" }),
  // Empty content discards the generated guide.
  saveRepoGuide: (projectId: number, repoId: number, content: string) =>
    request<RepoGuide>(`/api/projects/${projectId}/repos/${repoId}/guide`, put({ content })),

  // -- lessons elstin proposed from its runs -------------------------------
  // Only "accepted" ones are shown to the planner and dev. `text` is a
  // person's edit before accepting; both fields are optional but not both absent.
  inbox: () => request<Inbox>("/api/inbox"),
  projectInbox: (projectId: number) => request<Inbox>(`/api/projects/${projectId}/inbox`),
  listLessons: (projectId: number) => request<ProjectLesson[]>(`/api/projects/${projectId}/lessons`),
  updateLesson: (projectId: number, lessonId: number, body: { text?: string; status?: LessonStatus }) =>
    request<ProjectLesson>(`/api/projects/${projectId}/lessons/${lessonId}`, put(body)),
  deleteLesson: (projectId: number, lessonId: number) =>
    request<{ lesson_id: number; deleted: boolean }>(`/api/projects/${projectId}/lessons/${lessonId}`, { method: "DELETE" }),

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
  getConfigCategories: () => request<ConfigCategory[]>("/api/config/categories"),
  // With a project id, the suggestions come from that project's own provider.
  getModelOptions: (projectId?: number) =>
    request<ModelOptions>(projectId === undefined ? "/api/config/models" : `/api/config/models?project_id=${projectId}`),
  setGlobalConfig: (key: string, value: string) =>
    request<{ key: string; updated: boolean }>(`/api/config/${encodeURIComponent(key)}`, put({ value })),

  // -- epics ---------------------------------------------------------------
  listProjectEpics: (projectId: number) => request<Epic[]>(`/api/projects/${projectId}/epics`),
  syncEpic: (jiraKey: string, projectId: number) =>
    request<Epic>("/api/epics/sync", json({ jira_key: jiraKey, project_id: projectId })),
  syncEpicFields: (epicKey: string) => request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/sync`, { method: "POST" }),
  setEpicRepo: (epicKey: string, projectRepoId: number) =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/repo`, put({ project_repo_id: projectRepoId })),
  // Local only: nothing is changed in GitHub or JIRA. Answers with what went and what was left.
  removeEpic: (epicKey: string) =>
    request<RemoveTicketReport>(`/api/epics/${encodeURIComponent(epicKey)}`, { method: "DELETE" }),
  setEpicGate: (epicKey: string, manualGate: boolean) =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/gate`, put({ manual_gate: manualGate })),
  // The answer to an epic waiting to open its pull request: open it, or keep the branch local.
  decideEpicPr: (epicKey: string, action: "open" | "skip") =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/pr-approval`, json({ action })),
  // Forget what was found for tickets not started yet; the next poll looks again.
  refreshPriorWork: (epicKey: string) =>
    request<Epic>(`/api/epics/${encodeURIComponent(epicKey)}/prior-work/refresh`, { method: "POST" }),
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
  answerStoryQuestions: (storyKey: string, answers: ClarificationAnswerInput[]) =>
    request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/answer`, json({ answers })),
  // Which diverged branch this ticket continues from in one repo; null = start fresh.
  pickPriorBranch: (storyKey: string, repoLabel: string, branch: string | null) =>
    request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/prior-work`, json({ repo_label: repoLabel, branch })),
  retryStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/retry`, { method: "POST" }),
  // Plans a story waiting for approval again; its answers are kept.
  replanStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/replan`, { method: "POST" }),
  // Stops a run in flight; the story is left failed ("cancelled") so Retry starts it over.
  cancelStory: (storyKey: string) => request<Story>(`/api/stories/${encodeURIComponent(storyKey)}/cancel`, { method: "POST" }),
  // A message to the agent working on a story; it reads it at the start of its next turn.
  sendGuidance: (storyKey: string, message: string) =>
    request<{ story_key: string; pending: number }>(`/api/stories/${encodeURIComponent(storyKey)}/guidance`, json({ message })),
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
  getStoryPlan: (storyKey: string) => request<StoryPlan>(`/api/stories/${encodeURIComponent(storyKey)}/plan`),

  // -- PO sessions: drafting a backlog with the PO agent --------------------
  // What mode an epic would get (no key = a brainstorm), read from the ticket
  // source; nothing is started.
  previewPoMode: (projectId: number, epicKey: string) =>
    request<PoModePreview>(`/api/projects/${projectId}/po-mode?epic_key=${encodeURIComponent(epicKey)}`),
  // `mode: "bug"` asks for one Bug ticket; left out, the server picks the mode from the epic.
  startPoSession: (projectId: number, body: { brief: string; epic_key?: string | null; jira_project_key?: string | null; mode?: "bug" | null }) =>
    request<PoSession>(`/api/projects/${projectId}/po-sessions`, json(body)),
  listPoSessions: (projectId: number) => request<PoSessionSummary[]>(`/api/projects/${projectId}/po-sessions`),
  getPoSession: (sessionId: number) => request<PoSession>(`/api/po-sessions/${sessionId}`),
  // `version` is the draft version the edit was made over; the server refuses (409) one made over a stale draft.
  savePoDraft: (sessionId: number, draft: PoAnyDraft, version: number) =>
    request<PoSession>(`/api/po-sessions/${sessionId}/draft`, put({ draft, version })),
  sendPoMessage: (sessionId: number, message: string) =>
    request<PoSession & { delivery: "live" | "queued" }>(`/api/po-sessions/${sessionId}/messages`, json({ message })),
  answerPoQuestions: (sessionId: number, answers: PoAnswerInput[]) =>
    request<PoSession>(`/api/po-sessions/${sessionId}/answer`, json({ answers })),
  // Creates the draft in JIRA in the background — or, with `refs`, only those stories; 409 with
  // the reason when it cannot. A publish that stopped is pressed again and continues where it was.
  publishPoSession: (sessionId: number, jiraProjectKey?: string | null, refs?: string[]) =>
    request<PoSession>(`/api/po-sessions/${sessionId}/publish`, json({ jira_project_key: jiraProjectKey ?? null, ...(refs ? { refs } : {}) })),
  // Let the draft stand on the agent's own best guesses instead of answering its questions.
  dismissPoQuestions: (sessionId: number) =>
    request<PoSession>(`/api/po-sessions/${sessionId}/questions/dismiss`, { method: "POST" }),
  // Post one existing ticket's suggested edit to it as a comment; the ticket's text is not changed.
  postPoSuggestion: (sessionId: number, key: string) =>
    request<PoSession>(`/api/po-sessions/${sessionId}/suggestions/post`, json({ key })),
  cancelPoSession: (sessionId: number) => request<PoSession>(`/api/po-sessions/${sessionId}/cancel`, { method: "POST" }),
  deletePoSession: (sessionId: number) =>
    request<{ deleted: number }>(`/api/po-sessions/${sessionId}`, { method: "DELETE" }),
  // The draft as text, for "Copy as markdown" / "Copy as JSON" — works whether or not elstin may write to JIRA.
  exportPoSession: async (sessionId: number, format: "markdown" | "json"): Promise<string> => {
    const res = await fetch(`/api/po-sessions/${sessionId}/export?format=${format}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(body.detail || `Request failed (${res.status})`, res.status);
    }
    return format === "json" ? JSON.stringify(await res.json(), null, 2) : res.text();
  },
  // Not a fetch: EventSource opens it (see storyActivityStreamUrl).
  poActivityStreamUrl: (sessionId: number) => `/api/po-sessions/${sessionId}/stream`,
};
