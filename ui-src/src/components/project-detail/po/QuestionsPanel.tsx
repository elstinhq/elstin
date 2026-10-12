import { useState } from "react";
import { CircleHelp } from "lucide-react";
import { useAnswerPoQuestions, useDismissPoQuestions } from "../../../hooks/usePoSessions";
import type { PoAnswerInput } from "../../../lib/api";
import { useToast } from "../../../lib/toast";
import { cn } from "../../../lib/utils";
import type { PoSession } from "../../../types/api";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";

interface Choice {
  optionId: string | null;
  text: string;
}

// What the agent could not settle from the code and the documents. The draft was
// written on its best guess for each, so these are not blocking — answer them and
// it revises the draft, dismiss them and the draft stands on its guesses, or ignore
// them and edit it yourself. Every question needs an answer (an option, or your own
// words) in one go, like a story's clarification questions.
export function QuestionsPanel({ projectId, session }: { projectId: number; session: PoSession }) {
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const answer = useAnswerPoQuestions(projectId, session.id);
  const dismiss = useDismissPoQuestions(projectId, session.id);
  const { showToast } = useToast();
  const open = session.questions;
  if (open.length === 0 || !["awaiting_input", "ready"].includes(session.status)) return null;

  const complete = open.every((q) => {
    const c = choices[q.id];
    return c && (c.optionId || c.text.trim());
  });

  function submit() {
    const answers: PoAnswerInput[] = open.map((q) => ({
      question_id: q.id,
      option_id: choices[q.id]?.optionId ?? null,
      text: choices[q.id]?.text.trim() || null,
    }));
    answer.mutate(answers, {
      onSuccess: () => {
        setChoices({});
        showToast("Answered — the agent will revise the draft", "success");
      },
      onError: (err) => showToast(err.message || "Could not send the answers", "error"),
    });
  }

  return (
    <section aria-label="Questions from the agent" className="bg-card border-warning/60 flex flex-col gap-3 rounded-xl border p-4 shadow-xs">
      <div className="flex items-center gap-2">
        <CircleHelp className="text-muted-foreground size-4" aria-hidden="true" />
        <h2 className="flex-1 text-sm font-semibold">The agent asks</h2>
        {/* The server only drops questions while the agent is waiting on them. */}
        {session.status === "awaiting_input" ? (
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground text-xs underline underline-offset-3 disabled:opacity-50"
            title="Publish on the agent's best guesses instead"
            disabled={dismiss.isPending}
            onClick={() =>
              dismiss.mutate(undefined, {
                onSuccess: () => showToast("Questions dismissed — the draft stands on the agent's guesses", "success"),
                onError: (err) => showToast(err.message || "Could not dismiss the questions", "error"),
              })
            }
          >
            Dismiss
          </button>
        ) : null}
      </div>

      {open.map((q, i) => {
        const chosen = choices[q.id];
        const described = q.options.filter((option) => option.description);
        return (
          <div key={q.id} role="group" aria-labelledby={`po-q-${q.id}`} className={cn("flex flex-col gap-2", i > 0 && "border-t pt-3")}>
            <span id={`po-q-${q.id}`} className="text-sm">
              {q.question}
            </span>
            {q.context ? <p className="text-muted-foreground text-xs">{q.context}</p> : null}
            <div className="flex flex-wrap gap-1.5">
              {q.options.map((option) => (
                <Button
                  key={option.id}
                  type="button"
                  size="xs"
                  variant={chosen?.optionId === option.id ? "default" : "outline"}
                  aria-pressed={chosen?.optionId === option.id}
                  title={option.description || undefined}
                  onClick={() =>
                    setChoices((prev) => ({
                      ...prev,
                      [q.id]: { optionId: prev[q.id]?.optionId === option.id ? null : option.id, text: prev[q.id]?.text ?? "" },
                    }))
                  }
                >
                  {option.label}
                </Button>
              ))}
              <Input
                aria-label={`Your own answer to: ${q.question}`}
                className="h-7 min-w-40 flex-1 text-[13px]"
                placeholder="Or type an answer"
                value={chosen?.text ?? ""}
                onChange={(e) => setChoices((prev) => ({ ...prev, [q.id]: { optionId: prev[q.id]?.optionId ?? null, text: e.target.value } }))}
              />
            </div>
            {described.length > 0 ? (
              <ul className="text-muted-foreground flex flex-col gap-0.5 text-xs">
                {described.map((option) => (
                  <li key={option.id}>
                    <span className="text-foreground font-medium">{option.label}</span> — {option.description}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        );
      })}

      <div className="flex justify-end">
        <Button size="xs" disabled={!complete} loading={answer.isPending} onClick={submit}>
          Send answers
        </Button>
      </div>
    </section>
  );
}
