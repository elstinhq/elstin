import { useState } from "react";
import { Check, Pencil, Trash2, Undo2, X } from "lucide-react";
import { Tag } from "../shared/StatusTag";
import { useDeleteLesson, useLessons, useUpdateLesson } from "../../hooks/useLessons";
import { useProjectConfig } from "../../hooks/useProjectConfig";
import { useToast } from "../../lib/toast";
import type { LessonStatus, ProjectLesson } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Textarea } from "../ui/textarea";

// Mirrors elstin/services/lessons.py's MAX_LESSON_CHARS; the API enforces it.
const MAX_LESSON_CHARS = 300;

// Lessons elstin distilled from stories that needed retries. Each is a
// candidate until a person accepts it: the planner and dev read an accepted
// lesson as authoritative, so this panel is where a wrong one gets caught —
// the evidence quote is shown so it can be checked against what happened,
// and the text can be corrected before it is accepted.

function LessonRow({ projectId, lesson }: { projectId: number; lesson: ProjectLesson }) {
  const update = useUpdateLesson(projectId);
  const remove = useDeleteLesson(projectId);
  const { showToast } = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(lesson.text);
  const busy = update.isPending || remove.isPending;
  const trimmed = draft.trim();
  const draftProblem = !trimmed ? "A lesson cannot be empty" : trimmed.length > MAX_LESSON_CHARS ? `Over ${MAX_LESSON_CHARS} characters` : null;

  function setStatus(status: LessonStatus, done: string) {
    update.mutate(
      { lessonId: lesson.id, status },
      {
        onSuccess: () => showToast(done, "success"),
        onError: (err) => showToast(err.message || "Could not update the lesson", "error"),
      },
    );
  }

  // Saving an edit on a proposed lesson also accepts it: a person who took the
  // trouble to correct it has decided it should be used.
  function saveEdit() {
    const status = lesson.status === "proposed" ? ("accepted" as const) : undefined;
    update.mutate(
      { lessonId: lesson.id, text: trimmed, ...(status ? { status } : {}) },
      {
        onSuccess: () => {
          setEditing(false);
          showToast(status ? "Lesson edited and accepted" : "Lesson saved", "success");
        },
        onError: (err) => showToast(err.message || "Could not save the lesson", "error"),
      },
    );
  }

  return (
    <div className="border-border flex flex-col gap-1.5 rounded-md border p-2.5">
      {editing ? (
        <div className="flex flex-col gap-1.5">
          <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} className="min-h-20 text-sm" aria-label="Lesson text" />
          <div className="flex items-center justify-between gap-2">
            <span className={draftProblem ? "text-destructive text-xs" : "text-muted-foreground text-xs"}>
              {draftProblem ?? `${trimmed.length}/${MAX_LESSON_CHARS}`}
            </span>
            <div className="flex gap-1.5">
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => {
                  setDraft(lesson.text);
                  setEditing(false);
                }}
              >
                Cancel
              </Button>
              <Button size="sm" loading={update.isPending} disabled={busy || Boolean(draftProblem) || trimmed === lesson.text.trim()} onClick={saveEdit}>
                {lesson.status === "proposed" ? "Save & accept" : "Save"}
              </Button>
            </div>
          </div>
        </div>
      ) : (
        <p className="text-sm">{lesson.text}</p>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <Tag label={lesson.kind} appearance="secondary" />
        <Tag
          label={`seen in ${lesson.occurrences} ${lesson.occurrences === 1 ? "story" : "stories"}`}
          appearance="outline"
          title={lesson.evidence_story_keys.join(", ")}
        />
        {lesson.scope_globs.length ? (
          <Tag label={lesson.scope_globs.join(", ")} appearance="outline" title="Dev is shown this only when its plan touches a matching file" />
        ) : null}
      </div>

      {lesson.evidence_quote ? (
        <p className="text-muted-foreground border-border border-l-2 pl-2 text-xs italic" title="What the run recorded that this lesson is based on">
          “{lesson.evidence_quote}”
          {lesson.evidence_story_keys.length ? <span className="not-italic"> — {lesson.evidence_story_keys.join(", ")}</span> : null}
        </p>
      ) : null}

      {!editing ? (
        <div className="flex flex-wrap items-center gap-1.5">
          {lesson.status === "proposed" ? (
            <>
              <Button size="sm" disabled={busy} loading={update.isPending && update.variables?.status === "accepted"} onClick={() => setStatus("accepted", "Lesson accepted — agents will see it")}>
                <Check />
                Accept
              </Button>
              <Button variant="outline" size="sm" disabled={busy} loading={update.isPending && update.variables?.status === "rejected"} onClick={() => setStatus("rejected", "Lesson rejected")}>
                <X />
                Reject
              </Button>
            </>
          ) : null}
          {lesson.status === "accepted" ? (
            <Button variant="outline" size="sm" disabled={busy} loading={update.isPending && update.variables?.status === "rejected"} onClick={() => setStatus("rejected", "Lesson no longer used")}>
              <X />
              Stop using
            </Button>
          ) : null}
          {lesson.status === "rejected" ? (
            <Button variant="outline" size="sm" disabled={busy} loading={update.isPending && update.variables?.status === "proposed"} onClick={() => setStatus("proposed", "Lesson back for review")}>
              <Undo2 />
              Reconsider
            </Button>
          ) : null}
          {lesson.status !== "rejected" ? (
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setEditing(true)}>
              <Pencil />
              Edit
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="icon"
            className="ml-auto size-7"
            aria-label="Delete lesson"
            title={lesson.status === "rejected" ? "Delete it — the same lesson can then be proposed again" : "Delete it. Reject instead to stop it being proposed again"}
            disabled={busy}
            loading={remove.isPending}
            onClick={() =>
              remove.mutate(lesson.id, {
                onSuccess: () => showToast("Lesson deleted", "success"),
                onError: (err) => showToast(err.message || "Could not delete the lesson", "error"),
              })
            }
          >
            <Trash2 />
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function Section({ title, hint, lessons, projectId }: { title: string; hint?: string; lessons: ProjectLesson[]; projectId: number }) {
  if (!lessons.length) return null;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col">
        <span className="text-xs font-semibold tracking-wide uppercase">
          {title} <span className="text-muted-foreground font-normal">({lessons.length})</span>
        </span>
        {hint ? <span className="text-muted-foreground text-xs">{hint}</span> : null}
      </div>
      {lessons.map((lesson) => (
        <LessonRow key={lesson.id} projectId={projectId} lesson={lesson} />
      ))}
    </div>
  );
}

export function LessonsPanel({ projectId }: { projectId: number }) {
  const lessonsQuery = useLessons(projectId);
  const configQuery = useProjectConfig(projectId);
  const [showRejected, setShowRejected] = useState(false);
  const lessons = lessonsQuery.data ?? [];
  const proposed = lessons.filter((l) => l.status === "proposed");
  const accepted = lessons.filter((l) => l.status === "accepted");
  const rejected = lessons.filter((l) => l.status === "rejected");
  const captureField = configQuery.data?.find((f) => f.key === "capture_lessons");
  const captureOn = captureField ? String(captureField.value).toLowerCase() === "true" : false;

  return (
    <Card className="p-4">
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-sm font-medium">Lessons</span>
          {proposed.length ? <Tag label={`${proposed.length} to review`} appearance="warning" /> : null}
        </div>
        <p className="text-muted-foreground text-xs">
          What Elstin learned from stories that needed retries. Accept a lesson and the planner and dev read it as
          authoritative on later stories — fix anything wrong first. The repo itself is never changed.
        </p>

        {lessonsQuery.isError ? (
          <p className="text-destructive text-sm">Could not load lessons.</p>
        ) : lessons.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            {captureOn
              ? "None yet — a lesson appears after a story that needed retries merges."
              : "None yet, and capturing is off. Turn on “capture lessons” in this project's settings to have Elstin propose them."}
          </p>
        ) : (
          <div className="flex flex-col gap-4">
            <Section title="To review" hint="Not shown to any agent until accepted." lessons={proposed} projectId={projectId} />
            <Section title="In use" hint="Shown to the planner, and to dev when its files match." lessons={accepted} projectId={projectId} />
            {rejected.length ? (
              <div className="flex flex-col gap-2">
                <Button variant="link" size="sm" className="text-muted-foreground h-auto justify-start px-0 py-0 text-xs" onClick={() => setShowRejected((v) => !v)}>
                  {showRejected ? "Hide" : "Show"} {rejected.length} rejected
                </Button>
                {showRejected ? <Section title="Rejected" hint="Never proposed again." lessons={rejected} projectId={projectId} /> : null}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </Card>
  );
}
