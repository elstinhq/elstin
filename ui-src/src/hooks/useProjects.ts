import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useProjects() {
  return useQuery({ queryKey: queryKeys.projects(), queryFn: api.listProjects });
}

export function useProject(id: number) {
  return useQuery({ queryKey: queryKeys.project(id), queryFn: () => api.getProject(id) });
}

export function useSetTicketSource(id: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (ticketSource: string) => api.updateProject(id, { ticket_source: ticketSource }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.project(id) }),
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description: string; repo_url: string | null }) => api.createProject(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projects() }),
  });
}
