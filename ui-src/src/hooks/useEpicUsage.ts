import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

export function useEpicUsage(epicKey: string | null) {
  return useQuery({
    queryKey: queryKeys.epicUsage(epicKey ?? ""),
    queryFn: () => api.getEpicUsage(epicKey as string),
    enabled: epicKey != null,
  });
}
