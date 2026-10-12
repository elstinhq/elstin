import { useApproveStory, useRetryStory } from "../../hooks/useStories";
import { useDecideEpicPr } from "../../hooks/useEpics";
import { useUpdateLesson } from "../../hooks/useLessons";
import { Link } from "../../lib/router";
import { useToast } from "../../lib/toast";
import type { InboxItem } from "../../types/api";
import { Button } from "../ui/button";

/** Where an item's story or epic opens: its project's task table with the
 * drawer on it. From that page the link only changes the query string. */
export function itemHref(item: InboxItem): string {
  const base = `/projects/${item.project_id}`;
  if (item.kind === "po_draft") return `${base}/backlog/${item.session_id}`;
  if (item.kind === "lesson") return `${base}/lessons`;
  if (item.story_key) return `${base}/tasks?story=${encodeURIComponent(item.story_key)}`;
  if (item.epic_key) return `${base}/tasks?epic=${encodeURIComponent(item.epic_key)}`;
  return `${base}/tasks`;
}

function Actions({ item }: { item: InboxItem }) {
  const approve = useApproveStory(item.project_id);
  const retry = useRetryStory(item.project_id);
  const decidePr = useDecideEpicPr(item.project_id);
  const updateLesson = useUpdateLesson(item.project_id);
  const { showToast } = useToast();
  const fail = (fallback: string) => (err: Error) => showToast(err.message || fallback, "error");
  const key = item.key ?? item.story_key ?? item.epic_key ?? "";

  switch (item.kind) {
    case "plan_approval":
      return (
        <Button
          size="sm"
          loading={approve.isPending}
          aria-label={`Approve the plan for ${key}`}
          title="Approve the plan so the dev agent starts writing code"
          onClick={() => approve.mutate(item.story_key!, { onSuccess: () => showToast(`Approved ${key}`, "success"), onError: fail("Could not approve") })}
        >
          Approve
        </Button>
      );
    case "questions":
      return (
        <Button size="sm" variant="outline" asChild>
          <Link href={itemHref(item)}>Answer</Link>
        </Button>
      );
    case "external_change":
      return (
        <Button size="sm" variant="outline" asChild>
          <Link href={itemHref(item)}>Review</Link>
        </Button>
      );
    case "failed":
      return (
        <Button
          size="sm"
          variant="outline"
          loading={retry.isPending}
          onClick={() => retry.mutate(item.story_key!, { onSuccess: () => showToast(`${key} queued for retry`, "success"), onError: fail("Could not retry") })}
        >
          Retry
        </Button>
      );
    case "pr_approval":
      return (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            loading={decidePr.isPending && decidePr.variables?.action === "open"}
            disabled={decidePr.isPending}
            onClick={() =>
              decidePr.mutate(
                { epicKey: item.epic_key!, action: "open" },
                { onSuccess: () => showToast(`Opening the pull request for ${key}`, "success"), onError: fail("Could not record this decision") },
              )
            }
          >
            Open pull request
          </Button>
          <Button
            size="sm"
            variant="outline"
            loading={decidePr.isPending && decidePr.variables?.action === "skip"}
            disabled={decidePr.isPending}
            title="End the epic with its branch kept on this machine; no pull request is opened"
            onClick={() =>
              decidePr.mutate(
                { epicKey: item.epic_key!, action: "skip" },
                { onSuccess: () => showToast(`${key} kept local — no pull request`, "success"), onError: fail("Could not record this decision") },
              )
            }
          >
            Keep local
          </Button>
        </div>
      );
    case "lesson":
      return (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            loading={updateLesson.isPending && updateLesson.variables?.status === "accepted"}
            disabled={updateLesson.isPending}
            onClick={() =>
              updateLesson.mutate({ lessonId: item.lesson_id!, status: "accepted" }, { onSuccess: () => showToast("Lesson accepted", "success"), onError: fail("Could not accept") })
            }
          >
            Accept
          </Button>
          <Button
            size="sm"
            variant="outline"
            loading={updateLesson.isPending && updateLesson.variables?.status === "rejected"}
            disabled={updateLesson.isPending}
            onClick={() =>
              updateLesson.mutate({ lessonId: item.lesson_id!, status: "rejected" }, { onSuccess: () => showToast("Lesson rejected", "success"), onError: fail("Could not reject") })
            }
          >
            Reject
          </Button>
        </div>
      );
    case "po_draft":
      return (
        <Button size="sm" variant="outline" asChild>
          <Link href={itemHref(item)}>Open</Link>
        </Button>
      );
  }
}

// One thing waiting on a person: what it is, and the button that settles it.
// Lessons are read in full (the text is the decision); everything else is a
// ticket, shown by key and title, opening its drawer.
export function InboxItemRow({ item }: { item: InboxItem }) {
  const stacked = item.kind === "pr_approval" || item.kind === "lesson";
  return (
    <div className={stacked ? "flex flex-col gap-2.5" : "flex items-center justify-between gap-3"}>
      <div className="flex min-w-0 flex-col gap-0.5">
        {item.key || item.detail ? (
          <span className="text-muted-foreground truncate font-mono text-xs">
            {[item.key, item.detail].filter(Boolean).join(" · ")}
          </span>
        ) : null}
        {item.kind === "lesson" ? (
          <p className="text-sm leading-relaxed">{item.title}</p>
        ) : (
          <Link href={itemHref(item)} className="truncate text-sm hover:underline" title={item.title}>
            {item.title || item.key}
          </Link>
        )}
      </div>
      <div className="shrink-0">
        <Actions item={item} />
      </div>
    </div>
  );
}
