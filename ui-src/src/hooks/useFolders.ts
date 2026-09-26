import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useProjectRepos(projectId: number) {
  return useQuery({ queryKey: queryKeys.projectRepos(projectId), queryFn: () => api.listProjectRepos(projectId) });
}

export function useAddProjectRepo(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { url: string } | { path: string }) => api.addProjectRepo(projectId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectRepos(projectId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) });
    },
  });
}

export function useRemoveProjectRepo(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (repoId: number) => api.removeProjectRepo(projectId, repoId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectRepos(projectId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) });
    },
  });
}

export function useRepoGuide(projectId: number, repoId: number) {
  return useQuery({ queryKey: queryKeys.repoGuide(projectId, repoId), queryFn: () => api.getRepoGuide(projectId, repoId) });
}

// Both mutations answer with the guide as it now stands, so the cache is set
// from the response instead of refetched.
export function useGenerateRepoGuide(projectId: number, repoId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.generateRepoGuide(projectId, repoId),
    onSuccess: (guide) => queryClient.setQueryData(queryKeys.repoGuide(projectId, repoId), guide),
  });
}

export function useSaveRepoGuide(projectId: number, repoId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (content: string) => api.saveRepoGuide(projectId, repoId, content),
    onSuccess: (guide) => queryClient.setQueryData(queryKeys.repoGuide(projectId, repoId), guide),
  });
}
