import { useState } from "react";
import { useAnswerPoQuestions } from "../../../hooks/usePoSessions";
import type { PoAnswerInput } from "../../../lib/api";
import { useToast } from "../../../lib/toast";
import type { PoSession } from "../../../types/api";
import { Alert, AlertDescription, AlertTitle } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";

interface Choice {
  optionId: string | null;
  text: string;
}

// What the agent could not settle from the code and the documents. The draft was
// written on its best guess for each, so these are not blocking — answer them and
// it revises the draft, or ignore them and edit it yourself. Every question needs
// an answer (an option, or your own words) in one go, like a story's clarification
// questions.
export function QuestionsPanel({ projectId, session }: { projectId: number; session: PoSession }) {
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const answer = useAnswerPoQuestions(projectId, session.id);
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
    <Alert variant="warning" className="text-foreground block">
      <AlertTitle className="mb-2">The agent has {open.length === 1 ? "a question" : `${open.length} questions`}</AlertTitle>
      <AlertDescription className="flex flex-col gap-4">
        {open.map((q) => (
          <fieldset key={q.id} className="flex flex-col gap-1.5">
            <legend className="text-foreground text-sm font-medium">{q.question}</legend>
            {q.context ? <p className="text-muted-foreground text-xs">{q.context}</p> : null}
            {q.options.map((option) => (
              <label key={option.id} className="text-foreground flex items-start gap-2 text-sm">
                <input
                  type="radio"
                  name={`po-q-${q.id}`}
                  className="mt-1"
                  checked={choices[q.id]?.optionId === option.id}
                  onChange={() => setChoices((prev) => ({ ...prev, [q.id]: { optionId: option.id, text: prev[q.id]?.text ?? "" } }))}
                />
                <span>
                  <span className="font-medium">{option.label}</span>
                  {option.description ? <span className="text-muted-foreground"> — {option.description}</span> : null}
                </span>
              </label>
            ))}
            <Input
              aria-label={`Your own answer to: ${q.question}`}
              placeholder={q.options.length ? "…or add your own words" : "Your answer"}
              value={choices[q.id]?.text ?? ""}
              onChange={(e) => setChoices((prev) => ({ ...prev, [q.id]: { optionId: prev[q.id]?.optionId ?? null, text: e.target.value } }))}
            />
          </fieldset>
        ))}
        <Button className="w-fit" disabled={!complete} loading={answer.isPending} onClick={submit}>
          Send answers
        </Button>
      </AlertDescription>
    </Alert>
  );
}
