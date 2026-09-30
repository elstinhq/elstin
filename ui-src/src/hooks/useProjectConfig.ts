import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useProjectConfig(projectId: number) {
  return useQuery({ queryKey: queryKeys.projectConfig(projectId), queryFn: () => api.getProjectConfig(projectId) });
}

export function useSetProjectConfig(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.setProjectConfig(projectId, key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectConfig(projectId) }),
  });
}

export function useClearProjectConfig(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.clearProjectConfig(projectId, key),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectConfig(projectId) }),
  });
}

export function useGlobalConfig() {
  return useQuery({ queryKey: queryKeys.globalConfig(), queryFn: api.getGlobalConfig });
}

export function useSetGlobalConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.setGlobalConfig(key, value),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.globalConfig() }),
  });
}

// The sections never change while the page is open, so they are fetched once.
export function useConfigCategories() {
  return useQuery({ queryKey: queryKeys.configCategories(), queryFn: api.getConfigCategories, staleTime: Infinity });
}
