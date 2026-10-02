import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ClarificationAnswerInput } from "../lib/api";
import { queryKeys } from "../lib/queryClient";
import type { ActivityEvent, ExternalChangeAction } from "../types/api";

// Every story mutation ends with the same refresh the vanilla page did after
// each fetch — re-pull this project's epic list so waves/statuses/gates
// stay in sync (setStoryKind in particular re-levels the whole epic
// server-side, so every row's wave can change, not just the one story).
export function useStartStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.startStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useApproveStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.approveStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useAnswerStoryQuestions(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ storyKey, answers }: { storyKey: string; answers: ClarificationAnswerInput[] }) =>
      api.answerStoryQuestions(storyKey, answers),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function usePickPriorBranch(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ storyKey, repoLabel, branch }: { storyKey: string; repoLabel: string; branch: string | null }) =>
      api.pickPriorBranch(storyKey, repoLabel, branch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useRetryStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.retryStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useReplanStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.replanStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useCancelStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.cancelStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

// Not followed by a refetch of the epic list: a message changes nothing on the
// board, only the story's activity feed, which is pushed.
export function useSendGuidance() {
  return useMutation({
    mutationFn: ({ storyKey, message }: { storyKey: string; message: string }) => api.sendGuidance(storyKey, message),
  });
}

export function useSetStoryKind(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ storyKey, kind }: { storyKey: string; kind: string }) => api.setStoryKind(storyKey, kind),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useResolveExternalChange(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ storyKey, action }: { storyKey: string; action: ExternalChangeAction }) =>
      api.resolveExternalChange(storyKey, action),
    // A re-plan moves the story back to pending, so the whole epic list is
    // refreshed, not just the flag.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

export function useSyncStory(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (storyKey: string) => api.syncStory(storyKey),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectEpics(projectId) }),
  });
}

// How many events the feed keeps in memory. A long run can emit thousands
// (every tool call, every reasoning block); past a few hundred the older
// ones are scrollback nobody reads, and the full history is still one
// request away on the server.
const MAX_FEED_EVENTS = 500;

export type ActivityConnection = "connecting" | "live" | "reconnecting" | "closed";

function appendEvent(events: ActivityEvent[], incoming: ActivityEvent): ActivityEvent[] {
  const newest = events[events.length - 1];
  // Ids ascend, so one comparison covers every duplicate: a reconnect that
  // replays further back than we asked, or the server's own catch-up read
  // overlapping what it already pushed.
  if (newest && incoming.id <= newest.id) return events;
  const next = [...events, incoming];
  return next.length > MAX_FEED_EVENTS ? next.slice(next.length - MAX_FEED_EVENTS) : next;
}

/**
 * Live activity for one story, pushed over SSE.
 *
 * The one piece of server state in this app that isn't a TanStack query,
 * and deliberately so: the server decides when there is something new, so
 * there is no fetch to cache, dedupe or invalidate. The stream opens with
 * the run's history and then stays open, which means the feed shows what
 * an agent did the moment it does it instead of up to a poll interval
 * later — and a quiet run costs one idle socket rather than a re-read of
 * its whole history every few seconds.
 *
 * `enabled` is the dialog's open flag: the effect tears the connection
 * down when it goes false or the story changes, so a closed dialog leaves
 * nothing behind (a browser will otherwise hold an EventSource open, and
 * keep reconnecting it, for the life of the page).
 */
export function useStoryActivity(storyKey: string | null, enabled: boolean) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [connection, setConnection] = useState<ActivityConnection>("connecting");

  useEffect(() => {
    if (!enabled || storyKey == null) return;

    // Reopening on a different story must not show the previous one's tail.
    setEvents([]);
    setConnection("connecting");

    const source = new EventSource(api.storyActivityStreamUrl(storyKey));
    source.onopen = () => setConnection("live");
    source.onmessage = (message: MessageEvent<string>) => {
      let event: ActivityEvent;
      try {
        event = JSON.parse(message.data) as ActivityEvent;
      } catch {
        return; // a frame we can't read is not a reason to drop the stream
      }
      setEvents((prev) => appendEvent(prev, event));
    };
    // EventSource reports a dropped connection and a refused one the same
    // way; readyState is what separates "the browser is retrying" from
    // "it has given up" (an outright 404, say).
    source.onerror = () => setConnection(source.readyState === EventSource.CLOSED ? "closed" : "reconnecting");

    return () => source.close();
  }, [storyKey, enabled]);

  return { events, connection };
}

export function useStoryUsage(storyKey: string | null) {
  return useQuery({
    queryKey: queryKeys.storyUsage(storyKey ?? ""),
    queryFn: () => api.getStoryUsage(storyKey as string),
    enabled: storyKey != null,
  });
}

// Reopens the story's LangGraph checkpoint server-side — not free, so this
// is only fetched while the story detail dialog is actually open
// (`enabled`), same convention as useStoryActivity.
export function useStoryPlan(storyKey: string | null, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.storyPlan(storyKey ?? ""),
    queryFn: () => api.getStoryPlan(storyKey as string),
    enabled: enabled && storyKey != null,
  });
}
