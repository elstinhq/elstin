import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, type PoAnswerInput } from "../lib/api";
import { queryKeys } from "../lib/queryClient";
import type { PoAnyDraft, PoSession, PoStatus } from "../types/api";

// The agent works in the daemon, so a session moves on its own; the page finds
// out by polling. Quickly while something is happening, slowly once it is waiting
// on a person.
const BUSY: ReadonlySet<PoStatus> = new Set(["queued", "running", "publishing"]);

export function usePoSessions(projectId: number) {
  return useQuery({
    queryKey: queryKeys.poSessions(projectId),
    queryFn: () => api.listPoSessions(projectId),
    refetchInterval: 5000,
  });
}

// `enabled` lets a caller hold the poll until there is something to ask for.
export function usePoSession(sessionId: number | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.poSession(sessionId ?? 0),
    queryFn: () => api.getPoSession(sessionId as number),
    enabled: enabled && sessionId != null,
    // A draft that does not exist is an answer, not a failure to retry.
    retry: (failures, error) => !(error instanceof ApiError && error.status === 404) && failures < 3,
    refetchInterval: (query) => (query.state.data && BUSY.has(query.state.data.status) ? 2000 : 8000),
  });
}

// What mode a typed epic key would get. Only asked once there is a key, and
// never retried: a 422 ("that is a Story") is an answer, not a failure.
export function usePoModePreview(projectId: number, epicKey: string) {
  return useQuery({
    queryKey: queryKeys.poMode(projectId, epicKey),
    queryFn: () => api.previewPoMode(projectId, epicKey),
    enabled: epicKey.length > 0,
    retry: false,
    staleTime: 30_000,
  });
}

export function useStartPoSession(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { brief: string; epic_key?: string | null; jira_project_key?: string | null; mode?: "bug" | null }) =>
      api.startPoSession(projectId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.poSessions(projectId) }),
  });
}

// A mutation whose answer is the session: refetch it (the conversation is not
// in the answer) and the project's list, so the page shows the new state
// without waiting for the next poll.
function useSessionMutation<TVars, TData extends PoSession = PoSession>(
  projectId: number,
  sessionId: number,
  fn: (vars: TVars) => Promise<TData>,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.poSession(sessionId) });
      queryClient.invalidateQueries({ queryKey: queryKeys.poSessions(projectId) });
    },
  });
}

export function useSavePoDraft(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, ({ draft, version }: { draft: PoAnyDraft; version: number }) =>
    api.savePoDraft(sessionId, draft, version),
  );
}

export function useSendPoMessage(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, (message: string) => api.sendPoMessage(sessionId, message));
}

export function useAnswerPoQuestions(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, (answers: PoAnswerInput[]) => api.answerPoQuestions(sessionId, answers));
}

// Everything still not in JIRA, or — with `refs` — only those stories.
export function usePublishPoSession(projectId: number, sessionId: number) {
  const queryClient = useQueryClient();
  return useSessionMutation(projectId, sessionId, async ({ jiraProjectKey, refs }: { jiraProjectKey: string | null; refs?: string[] }) => {
    const saved = await api.publishPoSession(sessionId, jiraProjectKey, refs);
    // The answer is the session, already `publishing`: show that now, so the buttons that
    // would start a second publish are off at once rather than at the next poll.
    queryClient.setQueryData<PoSession>(queryKeys.poSession(sessionId), (old) => (old ? { ...old, ...saved, messages: old.messages } : old));
    return saved;
  });
}

export function useDismissPoQuestions(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, () => api.dismissPoQuestions(sessionId));
}

export function usePostPoSuggestion(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, (key: string) => api.postPoSuggestion(sessionId, key));
}

export function useCancelPoSession(projectId: number, sessionId: number) {
  return useSessionMutation(projectId, sessionId, () => api.cancelPoSession(sessionId));
}

export function useDeletePoSession(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sessionId: number) => api.deletePoSession(sessionId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.poSessions(projectId) }),
  });
}
