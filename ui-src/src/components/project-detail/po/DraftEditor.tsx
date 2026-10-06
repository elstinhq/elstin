import { useEffect, useMemo, useState } from "react";
import { ChevronsDownUp, ChevronsUpDown, Plus } from "lucide-react";
import { usePostPoSuggestion, usePublishPoSession, useSavePoDraft } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import type { PoAssessment, PoDraft, PoExisting, PoSession, PoStory } from "../../../types/api";
import { PO_ASSESSMENT_APPEARANCE, PO_ASSESSMENT_LABEL, Tag } from "../../shared/StatusTag";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Card } from "../../ui/card";
import { Input } from "../../ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../ui/select";
import { Textarea } from "../../ui/textarea";
import {
  defectsForStory, defectsOutsideStories, jiraUrl, NEEDS_PROJECT_KEY_REASON, newStory, nextRef, removeStory, updateStory, withoutBlanks,
} from "./draftOps";
import { EvidenceChips } from "./EvidenceChips";
import { PublishedKey, StoryCard, type StoryPublishControl } from "./StoryCard";
import { StringList } from "./StringList";

const EDITABLE = new Set(["ready", "awaiting_input", "failed"]);
const ASSESSMENTS = Object.keys(PO_ASSESSMENT_LABEL) as PoAssessment[];

function Section({ title, hint, action, children }: { title: React.ReactNode; hint?: string; action?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-end justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <h3 className="flex items-center gap-2 text-base font-semibold">{title}</h3>
          {hint ? <p className="text-muted-foreground text-xs">{hint}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function ExistingCard({
  row,
  summary,
  readOnly,
  jiraBaseUrl,
  onChange,
  suggestion,
}: {
  row: PoExisting;
  summary: string | undefined;
  readOnly: boolean;
  jiraBaseUrl: string | null;
  onChange: (patch: Partial<PoExisting>) => void;
  // Posting the suggestion as a comment on the ticket: whether it can be, whether it
  // already was, and the click. Absent when the session cannot post at all.
  suggestion?: { posted: boolean; allowed: boolean; pending: boolean; blocked: string | null; onPost: () => void };
}) {
  const href = jiraUrl(jiraBaseUrl, row.key);
  return (
    <Card className="gap-2.5 px-4" data-testid={`po-existing-${row.key}`}>
      <div className="flex flex-wrap items-center gap-2">
        {href ? (
          <a className="text-sm font-semibold underline-offset-2 hover:underline" href={href} target="_blank" rel="noreferrer">
            {row.key}
          </a>
        ) : (
          <span className="text-sm font-semibold">{row.key}</span>
        )}
        <span className="text-muted-foreground min-w-0 flex-1 truncate text-sm">{summary}</span>
        {readOnly ? (
          <Tag label={PO_ASSESSMENT_LABEL[row.assessment] ?? row.assessment} appearance={PO_ASSESSMENT_APPEARANCE[row.assessment]} />
        ) : (
          <Select value={row.assessment} onValueChange={(assessment) => onChange({ assessment: assessment as PoAssessment })}>
            <SelectTrigger size="sm" aria-label={`Assessment of ${row.key}`}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ASSESSMENTS.map((a) => (
                <SelectItem key={a} value={a}>
                  {PO_ASSESSMENT_LABEL[a]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </div>
      <EvidenceChips evidence={row.evidence} jiraBaseUrl={jiraBaseUrl} />
      <div className="flex flex-col gap-1">
        <span className="text-muted-foreground text-xs">
          Suggested edit — never applied to the ticket by itself; a person posts it as a comment.
        </span>
        <Textarea
          aria-label={`Suggested edit for ${row.key}`}
          className="min-h-9 py-1.5"
          placeholder="No change suggested"
          value={row.suggested_edit ?? ""}
          disabled={readOnly}
          onChange={(e) => onChange({ suggested_edit: e.target.value || null })}
        />
        {suggestion && row.suggested_edit?.trim() ? (
          suggestion.posted ? (
            <span className="text-success text-xs">Posted as a comment on {row.key}.</span>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" disabled={!suggestion.allowed} loading={suggestion.pending} onClick={suggestion.onPost}>
                Post as a comment on {row.key}
              </Button>
              {suggestion.blocked ? <span className="text-muted-foreground text-xs">{suggestion.blocked}</span> : null}
            </div>
          )
        ) : null}
      </div>
    </Card>
  );
}

interface DraftEditorProps {
  projectId: number;
  session: PoSession;
  jiraBaseUrl: string | null;
  // Told whenever there are (or stop being) unsaved edits — publishing is held back
  // while there are, since what is published is what is saved.
  onDirtyChange?: (dirty: boolean) => void;
  // The JIRA project typed beside the page's Publish button, for a story's own Publish
  // when the session does not know it yet.
  typedKey: string;
}

// The draft as a document a person edits. It is the draft — not a copy of it —
// so what is saved is what the agent is shown on its next revision, with the paths
// a person changed so their edits are not undone (elstin/services/po_sessions.py).
//
// Edits live here until Save: the page polls the session, and a poll must not
// overwrite what is half typed. When a newer draft arrives while edits are
// pending the person is told and chooses; nothing is merged behind their back.
export function DraftEditor({ projectId, session, jiraBaseUrl, onDirtyChange, typedKey }: DraftEditorProps) {
  const save = useSavePoDraft(projectId, session.id);
  const post = usePostPoSuggestion(projectId, session.id);
  const publishStory = usePublishPoSession(projectId, session.id);
  const { showToast } = useToast();
  // The story whose own Publish was pressed last; it shows as busy while a publish runs.
  const [pressed, setPressed] = useState<string | null>(null);
  // Stories folded open. A story with a problem starts open, so it is not missed.
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set((session.draft?.stories ?? []).filter((_, i) => defectsForStory(session.problems, i).length > 0).map((s) => s.ref)),
  );
  const [local, setLocal] = useState<PoDraft | null>(session.draft);
  const [base, setBase] = useState<{ draft: PoDraft | null; version: number }>({
    draft: session.draft,
    version: session.draft_version,
  });

  const dirty = useMemo(() => JSON.stringify(local) !== JSON.stringify(base.draft), [local, base.draft]);
  const behind = session.draft_version !== base.version;

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // A newer draft (the agent finished a turn) replaces the view only when nothing
  // is pending; otherwise `behind` says so below.
  useEffect(() => {
    if (!dirty && session.draft_version !== base.version) {
      setLocal(session.draft);
      setBase({ draft: session.draft, version: session.draft_version });
    }
  }, [session.draft_version, session.draft, dirty, base.version]);

  const readOnly = !EDITABLE.has(session.status);
  const mode = session.mode;

  const existingSummary = useMemo(() => new Map(session.existing_stories.map((s) => [s.key, s.summary])), [session.existing_stories]);

  if (!local) {
    return (
      <div className="text-muted-foreground flex h-full min-h-40 items-center justify-center text-sm">
        {session.status === "failed"
          ? "The agent could not produce a draft. Send it a message to try again."
          : "The agent has not produced a draft yet — it appears here as soon as it does."}
      </div>
    );
  }
  const draft = local;
  const created = session.publish_result?.created ?? {};
  const publishing = publishStory.isPending || session.status === "publishing";

  // Saved-draft findings point at positions in the *saved* draft, so while edits
  // are pending they could point at the wrong story.
  const problems = dirty ? [] : session.problems;
  const warnings = dirty ? [] : session.warnings;
  const unlinked = defectsOutsideStories([...problems, ...warnings]);

  function edit(next: PoDraft) {
    setLocal(next);
  }

  function saveNow() {
    if (!local) return;
    save.mutate(
      { draft: withoutBlanks(local), version: base.version },
      {
        onSuccess: (saved) => {
          setLocal(saved.draft);
          setBase({ draft: saved.draft, version: saved.draft_version });
          showToast("Draft saved", "success");
        },
        onError: (err) => showToast(err.message || "Could not save the draft", "error"),
      },
    );
  }

  function reload() {
    setLocal(session.draft);
    setBase({ draft: session.draft, version: session.draft_version });
  }

  const epicEditable = mode === "brainstorm" && !readOnly && !created.epic;

  function setOpen(ref: string, open: boolean) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (open) next.add(ref);
      else next.delete(ref);
      return next;
    });
  }
  const allOpen = draft.stories.length > 0 && draft.stories.every((s) => expanded.has(s.ref));

  // What is on screen against what is saved, for one story: its own Publish sends the
  // saved text, so it waits for that story's edits (not for the rest of the draft's).
  const savedStories = new Map((base.draft?.stories ?? []).map((s) => [s.ref, JSON.stringify(s)]));
  const epicUnsaved = JSON.stringify(draft.epic) !== JSON.stringify(base.draft?.epic);

  function pressPublish(ref: string) {
    setPressed(ref);
    publishStory.mutate(
      { jiraProjectKey: session.publish.jira_project_key ? null : typedKey.trim().toUpperCase() || null, refs: [ref] },
      {
        onSuccess: () => showToast(`Publishing ${ref} — it appears in JIRA in a moment`, "success"),
        onError: (err) => {
          setPressed(null);
          showToast(err.message || `Could not publish ${ref}`, "error");
        },
      },
    );
  }

  function publishControl(story: PoStory): StoryPublishControl | undefined {
    const state = session.story_publish?.[story.ref];
    if (!state || session.status === "discarded") return undefined;
    const typed = typedKey.trim();
    // The one reason a person clears by typing the project key beside the page's Publish button.
    const keyTyped = state.reason === NEEDS_PROJECT_KEY_REASON && typed !== "";
    let allowed = state.allowed || keyTyped;
    let reason = allowed ? null : state.reason;
    if (allowed && JSON.stringify(story) !== savedStories.get(story.ref)) {
      allowed = false;
      reason = "Save your changes to this story first: what is published is what is saved.";
    } else if (allowed && mode === "brainstorm" && !created.epic && epicUnsaved) {
      allowed = false;
      reason = "Save your changes to the epic first: it is created along with the first story.";
    }
    if (allowed && publishing) {
      allowed = false;
      reason = "Another publish is under way.";
    }
    return { allowed, reason, counts: state.counts, busy: pressed === story.ref && publishing, onPress: () => pressPublish(story.ref) };
  }
  const otherKeys = (ref: string) => [
    ...draft.stories.filter((s) => s.ref !== ref).map((s) => ({ key: s.ref, label: s.summary })),
    ...(mode === "gap" ? session.existing_stories.map((s) => ({ key: s.key, label: s.summary })) : []),
  ];

  return (
    <div className="flex flex-col gap-7">
      {unlinked.length > 0 ? (
        <Alert variant="warning" className="text-foreground">
          <AlertDescription>
            <ul className="flex list-disc flex-col gap-1 pl-4">
              {unlinked.map((d, i) => (
                <li key={i}>{d.reason}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      ) : null}
      {behind && dirty ? (
        <Alert variant="warning" className="text-foreground">
          <AlertDescription>
            The agent has updated the draft since you started editing. Saving now will be refused.{" "}
            <button type="button" className="underline" onClick={reload}>
              Discard my edits and show the new draft
            </button>
          </AlertDescription>
        </Alert>
      ) : null}

      <Section
        title={
          <>
            {mode === "brainstorm" ? "Epic" : `Epic ${session.epic_key ?? ""}`}
            {created.epic ? <PublishedKey ticketKey={created.epic} base={jiraBaseUrl} /> : mode === "brainstorm" ? <Tag label="new" appearance="secondary" /> : null}
          </>
        }
        hint={
          mode === "brainstorm"
            ? "Created in JIRA first, then the stories under it."
            : "The existing epic is never rewritten. A sharper description may be suggested below."
        }
      >
        <Card className="gap-3 px-4">
          <Input
            aria-label="Epic summary"
            className="font-medium"
            value={draft.epic.summary}
            disabled={!epicEditable}
            onChange={(e) => edit({ ...draft, epic: { ...draft.epic, summary: e.target.value } })}
          />
          <Textarea
            aria-label="Epic description"
            value={draft.epic.description}
            disabled={!epicEditable}
            onChange={(e) => edit({ ...draft, epic: { ...draft.epic, description: e.target.value } })}
          />
          <StringList
            label="Epic acceptance criteria"
            items={draft.epic.acceptance_criteria}
            readOnly={!epicEditable}
            addLabel="Add criterion"
            onChange={(acceptance_criteria) => edit({ ...draft, epic: { ...draft.epic, acceptance_criteria } })}
          />
          {draft.epic.suggested_description ? (
            <div className="bg-muted flex flex-col gap-1 rounded-md p-2.5">
              <span className="text-xs font-medium">Suggested sharper description</span>
              <p className="whitespace-pre-wrap text-sm">{draft.epic.suggested_description}</p>
              <span className="text-muted-foreground text-xs">Not applied to the epic: copy it across yourself if you want it.</span>
            </div>
          ) : null}
        </Card>
      </Section>

      {mode === "gap" ? (
        <Section title="Where the existing stories stand" hint="The agent's assessment of each, with what it found. You can correct a verdict.">
          {draft.existing.length === 0 ? <p className="text-muted-foreground text-sm">No assessments.</p> : null}
          {draft.existing.map((row) => (
            <ExistingCard
              key={row.key}
              row={row}
              summary={existingSummary.get(row.key)}
              readOnly={readOnly}
              jiraBaseUrl={jiraBaseUrl}
              onChange={(patch) => edit({ ...draft, existing: draft.existing.map((r) => (r.key === row.key ? { ...r, ...patch } : r)) })}
              suggestion={{
                posted: (session.publish_result?.comments ?? []).includes(row.key),
                // The text posted is the saved text, so unsaved edits must be saved first.
                allowed: session.publish.comments_allowed && !dirty && !["queued", "running", "publishing"].includes(session.status),
                pending: post.isPending && post.variables === row.key,
                blocked: !session.publish.comments_allowed
                  ? "Posting suggestions as comments is off for this project (po_jira_comment_enabled) — copy it across by hand."
                  : dirty
                    ? "Save your edits first."
                    : null,
                onPost: () =>
                  post.mutate(row.key, {
                    onSuccess: () => showToast(`Posted the suggestion on ${row.key}`, "success"),
                    onError: (err) => showToast(err.message || "Could not post the comment", "error"),
                  }),
              }}
            />
          ))}
          <StringList
            label="Gaps — promised somewhere, covered by no story"
            items={draft.gaps}
            readOnly={readOnly}
            emptyHint="None found."
            addLabel="Add a gap"
            onChange={(gaps) => edit({ ...draft, gaps })}
          />
        </Section>
      ) : null}

      <Section
        title={mode === "gap" ? `Proposed new stories (${draft.stories.length})` : `Stories (${draft.stories.length})`}
        hint="Each becomes one JIRA ticket, planned and built by its own Elstin run. Keep them small."
        action={
          draft.stories.length > 1 ? (
            <Button variant="ghost" size="sm" onClick={() => setExpanded(allOpen ? new Set() : new Set(draft.stories.map((s) => s.ref)))}>
              {allOpen ? <ChevronsDownUp /> : <ChevronsUpDown />}
              {allOpen ? "Collapse all" : "Expand all"}
            </Button>
          ) : null
        }
      >
        {draft.stories.length === 0 ? <p className="text-muted-foreground text-sm">No stories proposed.</p> : null}
        <div className="flex flex-col gap-2">
          {draft.stories.map((story, index) => (
            <StoryCard
              key={story.ref}
              story={story}
              index={index}
              others={otherKeys(story.ref)}
              defects={defectsForStory([...problems, ...warnings], index)}
              readOnly={readOnly}
              jiraBaseUrl={jiraBaseUrl}
              open={expanded.has(story.ref)}
              onOpenChange={(open) => setOpen(story.ref, open)}
              publishedKey={created[story.ref] ?? null}
              publish={publishControl(story)}
              onChange={(patch) => edit(updateStory(draft, story.ref, patch))}
              onRemove={() => edit(removeStory(draft, story.ref))}
            />
          ))}
        </div>
        {!readOnly ? (
          <Button
            variant="outline"
            className="w-fit"
            onClick={() => {
              const story = newStory(nextRef(draft.stories));
              setOpen(story.ref, true);
              edit({ ...draft, stories: [...draft.stories, story] });
            }}
          >
            <Plus />
            Add a story
          </Button>
        ) : null}
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
              onChange={(assumptions) => edit({ ...draft, assumptions })}
            />
          </Card>
          <Card className="px-4">
            <StringList
              label="Out of scope"
              items={draft.out_of_scope}
              readOnly={readOnly}
              emptyHint="Nothing ruled out."
              addLabel="Add"
              onChange={(out_of_scope) => edit({ ...draft, out_of_scope })}
            />
          </Card>
        </div>
      </Section>

      {dirty ? (
        <div className="bg-background/95 sticky bottom-0 z-10 -mx-1 flex flex-wrap items-center gap-2 border-t px-1 py-3 backdrop-blur">
          <span className="text-muted-foreground mr-auto text-xs">Unsaved edits. The agent sees them, and keeps them, on its next revision.</span>
          <Button variant="outline" onClick={reload} disabled={save.isPending}>
            Discard edits
          </Button>
          <Button onClick={saveNow} loading={save.isPending}>
            Save changes
          </Button>
        </div>
      ) : null}
    </div>
  );
}
