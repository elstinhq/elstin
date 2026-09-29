import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { queryKeys } from "../lib/queryClient";
import type { LessonStatus } from "../types/api";

export function useLessons(projectId: number) {
  return useQuery({ queryKey: queryKeys.projectLessons(projectId), queryFn: () => api.listLessons(projectId) });
}

// Both mutations refetch rather than patch the cache: the list is small and a
// verdict moves a lesson between the panel's sections.
export function useUpdateLesson(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ lessonId, ...body }: { lessonId: number; text?: string; status?: LessonStatus }) =>
      api.updateLesson(projectId, lessonId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectLessons(projectId) }),
  });
}

export function useDeleteLesson(projectId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (lessonId: number) => api.deleteLesson(projectId, lessonId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.projectLessons(projectId) }),
  });
}
