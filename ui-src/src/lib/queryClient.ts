import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient();

export const queryKeys = {
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
};
