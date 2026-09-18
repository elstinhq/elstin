import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useProjectRepos(projectId: number) {
  return useQuery({ queryKey: queryKeys.projectRepos(projectId), queryFn: () => api.listProjectRepos(projectId) });
}

export function useCloneProjectRepo(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (url: string) => api.cloneProjectRepo(projectId, { url }),
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
