import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";

// Every project's counts and everything waiting on a person, in one request —
// the sidebar's badges and both "Needs you" views read it. Polled like the
// board (useProjectEpics), and refetched after every successful mutation
// (lib/queryClient.ts).
export function useInbox() {
  return useQuery({ queryKey: queryKeys.inbox(), queryFn: api.inbox, refetchInterval: 5000 });
}
