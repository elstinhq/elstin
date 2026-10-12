import { useEffect, useMemo, useState } from "react";
import { useSavePoDraft } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import type { PoBug, PoBugCauseIn, PoBugConfidence, PoBugDraft, PoSession } from "../../../types/api";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Card } from "../../ui/card";
import { Input } from "../../ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../ui/select";
import { Textarea } from "../../ui/textarea";
import { Field, Section } from "./DraftEditor";
import { bugWithoutBlanks, isBugDraft } from "./draftOps";
import { EvidenceChips } from "./EvidenceChips";
import { PublishedKey } from "./StoryCard";
import { StringList } from "./StringList";

const EDITABLE = new Set(["ready", "awaiting_input", "failed"]);

// How sure the agent is, in the words its prompt defines them by (product_owner.py).
const CONFIDENCE_LABEL: Record<PoBugConfidence, string> = {
  high: "High — the code plainly does it",
  medium: "Medium — depends on data or settings",
  low: "Low — where, not yet why",
};

const CAUSE_IN_LABEL: Record<PoBugCauseIn, string> = {
  code: "Code",
  data: "Data",
  configuration: "Configuration",
  unknown: "Unknown",
};

const asBug = (draft: PoSession["draft"]): PoBugDraft | null => (isBugDraft(draft) ? draft : null);

interface BugEditorProps {
  projectId: number;
  session: PoSession;
  jiraBaseUrl: string | null;
  // Told whenever there are (or stop being) unsaved edits — publishing waits for a save.
  onDirtyChange?: (dirty: boolean) => void;
}

// A bug-mode session's draft: one Bug ticket, edited in place. The sections are the
// ticket's own, in the order its description will have them (po_sessions.bug_markdown),
// so what is on screen is what JIRA will show. Edits live here until Save, exactly as in
// DraftEditor: a poll never overwrites what is half typed, and a newer draft from the
// agent is announced rather than merged.
export function BugEditor({ projectId, session, jiraBaseUrl, onDirtyChange }: BugEditorProps) {
  const save = useSavePoDraft(projectId, session.id);
  const { showToast } = useToast();
  const sessionDraft = asBug(session.draft);
  const [local, setLocal] = useState<PoBugDraft | null>(sessionDraft);
  const [base, setBase] = useState<{ draft: PoBugDraft | null; version: number }>({ draft: sessionDraft, version: session.draft_version });

  const dirty = useMemo(() => JSON.stringify(local) !== JSON.stringify(base.draft), [local, base.draft]);
  const behind = session.draft_version !== base.version;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!dirty && session.draft_version !== base.version) {
      setLocal(sessionDraft);
      setBase({ draft: sessionDraft, version: session.draft_version });
    }
  }, [session.draft_version, sessionDraft, dirty, base.version]);

  if (!local) {
    return (
      <div className="text-muted-foreground flex h-full min-h-40 items-center justify-center text-sm">
        {session.status === "failed"
          ? "The agent could not finish the investigation. Send it a message to try again."
          : "The agent is tracing the bug through the code — its ticket appears here as soon as it is written."}
      </div>
    );
  }
  const draft = local;
  const bug = draft.bug;
  const createdKey = session.publish_result?.created?.bug ?? null;
  // Once the bug is in JIRA the ticket is the record; the draft only shows what was made.
  const readOnly = !EDITABLE.has(session.status) || createdKey != null;
  // Findings point into the saved draft; while edits are pending they may be stale.
  const findings = dirty ? [] : [...session.problems, ...session.warnings];

  function setBug(patch: Partial<PoBug>) {
    setLocal({ ...draft, bug: { ...bug, ...patch } });
  }

  function saveNow() {
    save.mutate(
      { draft: bugWithoutBlanks(draft), version: base.version },
      {
        onSuccess: (saved) => {
          const next = asBug(saved.draft);
          setLocal(next);
          setBase({ draft: next, version: saved.draft_version });
          showToast("Draft saved", "success");
        },
        onError: (err) => showToast(err.message || "Could not save the draft", "error"),
      },
    );
  }

  function reload() {
    setLocal(sessionDraft);
    setBase({ draft: sessionDraft, version: session.draft_version });
  }

  return (
    <div className="flex flex-col gap-7" data-testid="po-bug-editor">
      {findings.length > 0 ? (
        <Alert variant="warning" className="text-foreground">
          <AlertDescription>
            <ul className="flex list-disc flex-col gap-1 pl-4">
              {findings.map((d, i) => (
                <li key={i}>{d.reason}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      ) : null}
      {behind && dirty ? (
        <Alert variant="warning" className="text-foreground">
          <AlertDescription>
            The agent has updated the bug since you started editing. Saving now will be refused.{" "}
            <button type="button" className="underline" onClick={reload}>
              Discard my edits and show the new version
            </button>
          </AlertDescription>
        </Alert>
      ) : null}

      <Section
        title={
          <>
            Bug
            {createdKey ? <PublishedKey ticketKey={createdKey} base={jiraBaseUrl} /> : null}
          </>
        }
        hintBeside
        hint={session.epic_key ? `Becomes a JIRA Bug under ${session.epic_key}` : "Becomes one JIRA Bug, on its own"}
      >
        <Card className="gap-3.5 px-4">
          <Field id="bug-summary" label="Summary">
            <Input id="bug-summary" value={bug.summary} disabled={readOnly} onChange={(e) => setBug({ summary: e.target.value })} />
          </Field>
          <Field id="bug-observed" label="Observed">
            <Textarea id="bug-observed" value={bug.observed} disabled={readOnly} onChange={(e) => setBug({ observed: e.target.value })} />
          </Field>
          <Field id="bug-expected" label="Expected">
            <Textarea id="bug-expected" value={bug.expected} disabled={readOnly} onChange={(e) => setBug({ expected: e.target.value })} />
          </Field>
          <StringList
            label="Steps to reproduce"
            items={bug.steps_to_reproduce}
            readOnly={readOnly}
            emptyHint="None — the code does not say when it happens."
            addLabel="Add a step"
            onChange={(steps_to_reproduce) => setBug({ steps_to_reproduce })}
          />
          <StringList label="Labels" items={bug.labels} readOnly={readOnly} emptyHint="None." addLabel="Add a label" onChange={(labels) => setBug({ labels })} />
        </Card>
      </Section>

      <Section title="Cause" hint="What the agent traced in the code, how sure it is, and what would settle it.">
        <Card className="gap-3.5 px-4">
          <div className="grid gap-3.5 sm:grid-cols-2">
            <Field id="bug-confidence" label="Confidence">
              <Select value={bug.confidence} disabled={readOnly} onValueChange={(v) => setBug({ confidence: v as PoBugConfidence })}>
                <SelectTrigger id="bug-confidence" size="sm" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(CONFIDENCE_LABEL) as PoBugConfidence[]).map((c) => (
                    <SelectItem key={c} value={c}>
                      {CONFIDENCE_LABEL[c]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field id="bug-cause-in" label="The cause is in">
              <Select value={bug.cause_in} disabled={readOnly} onValueChange={(v) => setBug({ cause_in: v as PoBugCauseIn })}>
                <SelectTrigger id="bug-cause-in" size="sm" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(CAUSE_IN_LABEL) as PoBugCauseIn[]).map((c) => (
                    <SelectItem key={c} value={c}>
                      {CAUSE_IN_LABEL[c]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>
          <Field id="bug-root-cause" label="Root cause">
            <Textarea
              id="bug-root-cause"
              className="min-h-32"
              value={bug.root_cause}
              disabled={readOnly}
              onChange={(e) => setBug({ root_cause: e.target.value })}
            />
          </Field>
          <Field id="bug-fix-location" label="Where the fix lies">
            <Textarea id="bug-fix-location" value={bug.fix_location} disabled={readOnly} onChange={(e) => setBug({ fix_location: e.target.value })} />
          </Field>
          <EvidenceChips
            label="Evidence"
            evidence={bug.evidence}
            jiraBaseUrl={jiraBaseUrl}
            onRemove={readOnly ? undefined : (index) => setBug({ evidence: bug.evidence.filter((_, i) => i !== index) })}
          />
          <StringList
            label="To confirm outside the code — a query, a log, a record to look at"
            items={bug.to_confirm}
            readOnly={readOnly}
            emptyHint="Nothing — the code settles it."
            addLabel="Add a check"
            onChange={(to_confirm) => setBug({ to_confirm })}
          />
        </Card>
      </Section>

      <Section title="Acceptance criteria" hint="What the reviewer holds the fix to: checkable from the diff, the build and the tests.">
        <Card className="px-4">
          <StringList
            label="Criteria"
            items={bug.acceptance_criteria}
            readOnly={readOnly}
            addLabel="Add criterion"
            onChange={(acceptance_criteria) => setBug({ acceptance_criteria })}
          />
        </Card>
      </Section>

      <Section title="Assumptions and scope">
        <div className="grid gap-3 md:grid-cols-2">
          <Card className="px-4">
            <StringList
              label="Assumptions"
              items={draft.assumptions}
              readOnly={readOnly}
              emptyHint="None recorded."
              addLabel="Add an assumption"
              onChange={(assumptions) => setLocal({ ...draft, assumptions })}
            />
          </Card>
          <Card className="px-4">
            <StringList
              label="Out of scope"
              items={draft.out_of_scope}
              readOnly={readOnly}
              emptyHint="Nothing ruled out."
              addLabel="Add"
              onChange={(out_of_scope) => setLocal({ ...draft, out_of_scope })}
            />
          </Card>
        </div>
      </Section>

      {dirty ? (
        <div className="bg-card sticky bottom-0 z-10 flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 shadow-lg">
          <span className="bg-warning size-2 shrink-0 rounded-full" aria-hidden="true" />
          <span className="min-w-0 flex-1 text-sm" title="The agent sees these edits, and keeps them, on its next revision.">
            Unsaved edits to the bug
          </span>
          <Button variant="ghost" size="xs" onClick={reload} disabled={save.isPending}>
            Discard edits
          </Button>
          <Button size="xs" onClick={saveNow} loading={save.isPending}>
            Save draft
          </Button>
        </div>
      ) : null}
    </div>
  );
}
