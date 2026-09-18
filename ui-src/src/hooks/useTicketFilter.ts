import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";
import type { TicketFilterInput } from "../types/api";

export function useTicketFilter(projectId: number) {
  return useQuery({
    queryKey: queryKeys.ticketFilter(projectId),
    queryFn: () => api.getTicketFilter(projectId).then((r) => r.filter),
  });
}

export function useSaveTicketFilter(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: TicketFilterInput) => api.setTicketFilter(projectId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.ticketFilter(projectId) }),
  });
}

export function useClearTicketFilter(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.clearTicketFilter(projectId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.ticketFilter(projectId) }),
  });
}

/** Dry run — nothing is written, so nothing is invalidated. */
export function usePreviewTicketFilter(projectId: number) {
  return useMutation({
    mutationFn: (filter?: TicketFilterInput) => api.previewTicketFilter(projectId, filter),
  });
}

/** A sweep both ingests epics and stamps the filter's last-synced info, so
 *  the epic list and the filter itself are both stale afterwards. */
export function useRunTicketFilterSync(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (filter?: TicketFilterInput) => api.runTicketFilterSync(projectId, filter),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.ticketFilter(projectId) });
    },
  });
}
