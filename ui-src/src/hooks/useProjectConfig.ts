import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

// Where a model setting's suggestions come from. Changing any of these changes
// which provider is asked, so the suggestions already fetched are someone
// else's. (Server side: elstin/llm/registry.py.)
const CONNECTION_KEYS = new Set(["llm_provider", "llm_base_url", "openai_api_key"]);

export function useProjectConfig(projectId: number) {
  return useQuery({ queryKey: queryKeys.projectConfig(projectId), queryFn: () => api.getProjectConfig(projectId) });
}

export function useSetProjectConfig(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.setProjectConfig(projectId, key, value),
    onSuccess: (_data, { key }) => {
      if (CONNECTION_KEYS.has(key)) queryClient.invalidateQueries({ queryKey: queryKeys.modelOptions(projectId) });
      return queryClient.invalidateQueries({ queryKey: queryKeys.projectConfig(projectId) });
    },
  });
}

export function useClearProjectConfig(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.clearProjectConfig(projectId, key),
    onSuccess: (_data, key) => {
      if (CONNECTION_KEYS.has(key)) queryClient.invalidateQueries({ queryKey: queryKeys.modelOptions(projectId) });
      return queryClient.invalidateQueries({ queryKey: queryKeys.projectConfig(projectId) });
    },
  });
}

export function useGlobalConfig() {
  return useQuery({ queryKey: queryKeys.globalConfig(), queryFn: api.getGlobalConfig });
}

export function useSetGlobalConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ key, value }: { key: string; value: string }) => api.setGlobalConfig(key, value),
    onSuccess: (_data, { key }) => {
      // Every project that has not chosen its own connection inherits this one.
      if (CONNECTION_KEYS.has(key)) queryClient.invalidateQueries({ queryKey: ["model-options"] });
      return queryClient.invalidateQueries({ queryKey: queryKeys.globalConfig() });
    },
  });
}

// The sections never change while the page is open, so they are fetched once.
export function useConfigCategories() {
  return useQuery({ queryKey: queryKeys.configCategories(), queryFn: api.getConfigCategories, staleTime: Infinity });
}

// Suggestions for a model field. Not refetched on window focus or mount: it is
// a round trip to the provider, and the server caches it for a few minutes
// anyway.
export function useModelOptions(projectId?: number) {
  return useQuery({
    queryKey: queryKeys.modelOptions(projectId),
    queryFn: () => api.getModelOptions(projectId),
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}
