import { MutationCache, QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";

// Any mutation can clear (or create) something waiting on a person — approving
// a plan, accepting a lesson, opening a pull request — so every successful one
// refreshes the inbox the sidebar and "Needs you" read, rather than each hook
// having to remember to.
//
// A 4xx is an answer, not a blip — a project or draft that does not exist will
// not exist on the third try either — so only other failures are retried.
export const queryClient: QueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failures, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && failures < 3,
    },
  },
  mutationCache: new MutationCache({
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["inbox"] }),
  }),
});

export const queryKeys = {
  inbox: () => ["inbox"] as const,
  projects: () => ["projects"] as const,
  project: (id: number) => ["projects", id] as const,
  projectRepos: (id: number) => ["projects", id, "repos"] as const,
  repoGuide: (projectId: number, repoId: number) => ["projects", projectId, "repos", repoId, "guide"] as const,
  projectLessons: (id: number) => ["projects", id, "lessons"] as const,
  projectEpics: (id: number) => ["projects", id, "epics"] as const,
  projectConfig: (id: number) => ["projects", id, "config"] as const,
  ticketFilter: (id: number) => ["projects", id, "ticket-filter"] as const,
  globalConfig: () => ["config"] as const,
  configCategories: () => ["config-categories"] as const,
  modelOptions: (projectId?: number) => ["model-options", projectId ?? "global"] as const,
  epicUsage: (epicKey: string) => ["epics", epicKey, "usage"] as const,
  storyUsage: (storyKey: string) => ["stories", storyKey, "usage"] as const,
  storyPlan: (storyKey: string) => ["stories", storyKey, "plan"] as const,
  poSessions: (projectId: number) => ["projects", projectId, "po-sessions"] as const,
  poSession: (sessionId: number) => ["po-sessions", sessionId] as const,
  poMode: (projectId: number, epicKey: string) => ["projects", projectId, "po-mode", epicKey] as const,
};
