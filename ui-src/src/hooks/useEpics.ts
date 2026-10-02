import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useProjectEpics(projectId: number) {
  return useQuery({
    queryKey: queryKeys.projectEpics(projectId),
    queryFn: () => api.listProjectEpics(projectId),
    refetchInterval: 5000,
  });
}

export function useSyncEpic(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jiraKey: string) => api.syncEpic(jiraKey, projectId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useSyncEpicFields(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (epicKey: string) => api.syncEpicFields(epicKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useSetEpicGate(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ epicKey, manualGate }: { epicKey: string; manualGate: boolean }) =>
      api.setEpicGate(epicKey, manualGate),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useDecideEpicPr(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ epicKey, action }: { epicKey: string; action: "open" | "skip" }) => api.decideEpicPr(epicKey, action),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useRefreshPriorWork(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (epicKey: string) => api.refreshPriorWork(epicKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useRemoveEpic(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (epicKey: string) => api.removeEpic(epicKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}
