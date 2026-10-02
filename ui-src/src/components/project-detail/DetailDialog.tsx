import { useState, type ReactNode } from "react";
import { SquareArrowOutUpRight } from "lucide-react";
import { PhaseTagFor, StatusTagFor } from "../shared/StatusTag";
import { ActivityFeed } from "./ActivityFeed";
import { StoryPlanPanel } from "./PlanPanel";
import { EpicUsagePanel, StoryUsagePanel } from "./UsagePanel";
import {
  useAnswerStoryQuestions,
  useReplanStory,
  useResolveExternalChange,
  useSetStoryKind,
  useSyncStory,
} from "../../hooks/useStories";
import { lookupDue, startCandidate } from "../../lib/priorWork";
import { useToast } from "../../lib/toast";
import { displayKey, sourceLabel, ticketUrl } from "../../lib/ticketSource";
import { useSyncEpicFields } from "../../hooks/useEpics";
import type { Epic, ExternalChangeAction, PriorWork, Story } from "../../types/api";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Label } from "../ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { GuidanceForm } from "./GuidanceForm";

export interface DetailTarget {
  kind: "epic" | "story";
  key: string;
}

interface DetailDialogProps {
  projectId: number;
  epics: Epic[];
  target: DetailTarget | null;
  onClose: () => void;
  jiraBaseUrl: string | null;
  ticketSource: string;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-0.5">
      <span className="text-muted-foreground text-xs">{label}</span>
      <div className="text-sm">{children ?? "—"}</div>
    </div>
  );
}

function DescriptionRow({ text }: { text: string }) {
  return (
    <DetailRow label="Description">
      <div className="max-h-[220px] overflow-y-auto whitespace-pre-wrap">{text}</div>
    </DetailRow>
  );
}

function KindDetailValue({ projectId, story }: { projectId: number; story: Story }) {
  const setKind = useSetStoryKind(projectId);
  const kind = story.kind || "dev";

  // Only a pending top-level story can be reclassified. A sub-task is
  // never dispatched, so it has no route to choose; and moving an
  // in-flight story's wave would desync the epic's levels from what has
  // already run. The API refuses both, so the control isn't offered.
  const canReclassify = story.status === "pending" && !story.parent_key;
  const next = kind === "qa" ? "dev" : "qa";

  return (
    <div className="flex items-center gap-2">
      <span>
        {story.parent_key ? "Sub-task — context only, never dispatched" : kind === "qa" ? "QA / validation" : "Implementation"}
      </span>
      {/* Showing which rule fired matters: the classification is inferred
          from the ticket's prose, so a human deciding whether to override it
          needs to see what forge read, not just what it concluded. */}
      {story.kind_reason ? <span className="text-muted-foreground text-xs">({story.kind_reason})</span> : null}
      {canReclassify ? (
        <Button variant="outline" size="sm" loading={setKind.isPending} onClick={() => setKind.mutate({ storyKey: story.jira_key, kind: next })}>
          {next === "qa" ? "Mark as QA" : "Mark as implementation"}
        </Button>
      ) : null}
    </div>
  );
}

// A story can only be re-planned while nothing is in flight to orphan — the
// same rule the API enforces, so the button is not offered when it would 409.
const REPLANNABLE = new Set(["awaiting_plan_approval", "failed"]);

function ChangedField({ label, current, incoming }: { label: string; current: string | null; incoming: string }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground text-xs">{label}</span>
      <div className="text-muted-foreground max-h-[120px] overflow-y-auto whitespace-pre-wrap text-xs line-through">{current || "—"}</div>
      <div className="max-h-[160px] overflow-y-auto whitespace-pre-wrap text-sm">{incoming || "—"}</div>
    </div>
  );
}

function ExternalChangePanel({ projectId, story }: { projectId: number; story: Story }) {
  const resolve = useResolveExternalChange(projectId);
  const { showToast } = useToast();
  const pending = story.external_pending;
  if (!pending) return null;

  const canReplan = REPLANNABLE.has(story.status);

  function run(action: ExternalChangeAction, done: string) {
    resolve.mutate(
      { storyKey: story.jira_key, action },
      {
        onSuccess: () => showToast(done, "success"),
        onError: (err) => showToast(err.message || "Could not resolve this change.", "error"),
      },
    );
  }

  return (
    <div className="border-border bg-accent/40 flex flex-col gap-3 rounded-md border p-3">
      <div className="text-sm font-medium">Changed in the source after this story was planned</div>
      <div className="text-muted-foreground text-xs">
        {pending.reasons.join("; ")}. Nothing has been rewritten under the plan or the run — choose what to do with it.
      </div>
      {pending.summary !== undefined ? <ChangedField label="Summary" current={story.summary} incoming={pending.summary} /> : null}
      {pending.description !== undefined ? (
        <ChangedField label="Description" current={story.description} incoming={pending.description} />
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" loading={resolve.isPending} onClick={() => run("accept", "New text applied; the plan is unchanged")}>
          Apply, keep the plan
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!canReplan || resolve.isPending}
          title={canReplan ? undefined : "Work is already under way or a PR exists — a re-plan would orphan it"}
          onClick={() => run("replan", "New text applied; the story will be planned again")}
        >
          Apply and re-plan
        </Button>
        <Button size="sm" variant="ghost" disabled={resolve.isPending} onClick={() => run("dismiss", "Change dismissed")}>
          Keep current text
        </Button>
      </div>
    </div>
  );
}

function ClarificationPanel({ projectId, story }: { projectId: number; story: Story }) {
  const answer = useAnswerStoryQuestions(projectId);
  const { showToast } = useToast();
  const questions = story.clarification_questions;
  const [picks, setPicks] = useState<Record<string, string>>({});
  if (!questions.length) return null;

  const complete = questions.every((q) => picks[q.id]);

  function submit() {
    if (!complete) return;
    answer.mutate(
      { storyKey: story.jira_key, answers: questions.map((q) => ({ question_id: q.id, option_id: picks[q.id]! })) },
      {
        onSuccess: () => showToast("Answer submitted — planning resumes", "success"),
        onError: (err) => showToast(err.message || "Could not submit this answer.", "error"),
      },
    );
  }

  return (
    <div className="border-border bg-accent/40 flex flex-col gap-4 rounded-md border p-3">
      <div className="text-sm font-medium">The planner needs a decision before it can finish this plan</div>
      {questions.map((q) => (
        <div key={q.id} className="flex flex-col gap-1.5">
          <Label htmlFor={`clarify-${q.id}`}>{q.question}</Label>
          {q.context ? <div className="text-muted-foreground text-xs">{q.context}</div> : null}
          <Select value={picks[q.id] ?? ""} onValueChange={(value) => setPicks((prev) => ({ ...prev, [q.id]: value }))}>
            <SelectTrigger id={`clarify-${q.id}`} className="w-full">
              <SelectValue placeholder="Choose an option" />
            </SelectTrigger>
            <SelectContent>
              {q.options.map((opt) => (
                <SelectItem key={opt.id} value={opt.id}>
                  {opt.label}
                  {opt.description ? ` — ${opt.description}` : ""}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      ))}
      <div>
        <Button size="sm" disabled={!complete} loading={answer.isPending} onClick={submit}>
          Submit answer
        </Button>
      </div>
    </div>
  );
}

// What already existed for this ticket before forge made its branch
// (forge/services/prior_work.py), per repo: the branch its work continues
// from, the other branches found, and work already in the default branch.
function PriorWorkRow({ prior }: { prior: PriorWork | null }) {
  if (!prior) return null;
  if (lookupDue(prior)) {
    return (
      <DetailRow label="Earlier work">
        <span className="text-muted-foreground">Looking again on the next poll…</span>
      </DetailRow>
    );
  }
  const repos = Object.entries(prior.repos ?? {}).sort(([a], [b]) => a.localeCompare(b));
  if (!repos.length) {
    return (
      <DetailRow label="Earlier work">
        <span className="text-muted-foreground">
          {prior.applicable ? "No branch or commit for this ticket was found" : "Not looked up — only JIRA-style keys appear in branch names"}
          {prior.looked_at ? ` (looked ${new Date(prior.looked_at).toLocaleString()})` : ""}
        </span>
      </DetailRow>
    );
  }
  return (
    <DetailRow label="Earlier work">
      <div className="flex flex-col gap-3">
        {repos.map(([label, finding]) => {
          const start = startCandidate(prior, label);
          return (
            <div key={label} className="flex flex-col gap-1">
              <div className="text-xs font-medium">
                {label}:{" "}
                {start
                  ? `continues ${start.branch}`
                  : finding.verdict === "choose"
                    ? label in (prior.picks ?? {}) ? "starts fresh (picked)" : "several diverged branches — pick one on the row"
                    : `already in ${finding.default_branch}`}
              </div>
              {finding.candidates.map((c) => (
                <div key={c.branch} className="text-muted-foreground text-xs">
                  <span className="font-mono">{c.branch}</span>
                  {c.in_default ? ` — in ${finding.default_branch}` : ` — ${c.ahead} commit(s) ahead, ${c.files_total} file(s)`}
                  {c.pull_request ? (
                    <>
                      {" · "}
                      <a className="text-primary underline underline-offset-2" href={c.pull_request.url} target="_blank" rel="noopener noreferrer">
                        PR #{c.pull_request.number} ({c.pull_request.state})
                      </a>
                    </>
                  ) : null}
                  {start && start.branch === c.branch && c.commits.length ? (
                    <ul className="mt-1 list-none font-mono text-[11px]">
                      {c.commits.map((commit) => (
                        <li key={commit} className="truncate">{commit}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ))}
              {finding.default_commits.length ? (
                <div className="text-muted-foreground text-xs">
                  Commits in {finding.default_branch} naming it: {finding.default_commits.slice(0, 3).join("; ")}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </DetailRow>
  );
}

function EpicDetailBody({ epic }: { epic: Epic }) {
  return (
    <>
      <DetailRow label="Ticket">{epic.external_id || epic.jira_key}</DetailRow>
      <DetailRow label="Summary">{epic.summary || "—"}</DetailRow>
      {epic.description ? <DescriptionRow text={epic.description} /> : null}
      <DetailRow label="Phase">
        <PhaseTagFor phase={epic.phase} />
      </DetailRow>
      {epic.external_status ? (
        <DetailRow label="Status in source">
          {epic.external_closed ? `${epic.external_status} — closed, so forge put this epic on hold` : epic.external_status}
        </DetailRow>
      ) : null}
      <DetailRow label="Folder">{epic.repo || "No folder attached"}</DetailRow>
      <DetailRow label="Plan approved">{epic.plan_approved ? "Yes" : "No"}</DetailRow>
      <DetailRow label="Manually paused">{epic.manual_gate ? "Yes" : "No"}</DetailRow>
      <DetailRow label="Integration branch">{epic.integration_branch}</DetailRow>
      <PriorWorkRow prior={epic.prior_work} />
      <DetailRow label="Waves">{(epic.levels || []).length ? String(epic.levels.length) : "0"}</DetailRow>
      <DetailRow label="Stories">{String((epic.stories || []).length)}</DetailRow>
      <EpicUsagePanel epicKey={epic.jira_key} />
    </>
  );
}

// A "failed" story that never got past planning and one where dev wrote
// real code but the build/reviewer rejected it look identical in the
// status tag alone — this is the same distinction CodeWrittenTag makes in
// the compact row, spelled out with the actual counters once someone has
// already clicked in to look.
function RunOutcomeRow({ story }: { story: Story }) {
  const outcome = story.run_outcome;
  if (!outcome || (outcome.dev_passes <= 0 && outcome.gate_runs <= 0)) return null;
  const parts = [
    `${outcome.dev_passes} dev pass(es)`,
    outcome.gate_runs ? `${outcome.gate_failures}/${outcome.gate_runs} build gate failure(s)` : null,
    outcome.verifier_runs ? `${outcome.verifier_rejections}/${outcome.verifier_runs} review rejection(s)` : null,
  ].filter(Boolean);
  return (
    <DetailRow label="Run outcome">
      <span className="text-amber-500">Code was written before this run failed</span> — {parts.join(", ")}.
      {outcome.gate_failures > 0 && !outcome.verifier_runs ? (
        <div className="text-muted-foreground mt-1 text-xs">
          Failed at the build/test gate before reaching review — often an environment issue (toolchain version,
          missing dependency) rather than the code itself.
        </div>
      ) : null}
    </DetailRow>
  );
}

// "Not this plan", short of approving it: the story is planned again from
// scratch. Answers already given are kept — they are decisions — and the new
// run is given them.
function ReplanRow({ projectId, story }: { projectId: number; story: Story }) {
  const replan = useReplanStory(projectId);
  const { showToast } = useToast();
  if (story.status !== "awaiting_plan_approval" || story.clarification_questions.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        variant="outline"
        size="sm"
        loading={replan.isPending}
        onClick={() =>
          replan.mutate(story.jira_key, {
            onSuccess: () => showToast(`${story.jira_key} will be planned again`, "success"),
            onError: (err) => showToast(err.message || "Could not re-plan this story.", "error"),
          })
        }
      >
        Re-plan
      </Button>
      <span className="text-muted-foreground text-xs">
        Discard this plan and make a new one{story.clarification_answers.length ? ", keeping your answers" : ""}.
      </span>
    </div>
  );
}

// Beside the plan, since approving it means accepting that these are left to
// a person: the epic's pull request lists them too.
function ReadOnlyChangesRow({ story }: { story: Story }) {
  const changes = story.read_only_changes ?? [];
  if (!changes.length) return null;
  return (
    <DetailRow label="Needs a person">
      <div className="text-muted-foreground text-xs">
        Changes this ticket needs that forge won't make — in read-only repos, or files the project says agents must
        not change. The rest of the plan runs without them.
      </div>
      <ul className="mt-1 flex flex-col gap-1">
        {changes.map((change) => (
          <li key={change.path} className="min-w-0 text-xs">
            <span className="font-mono break-all">{change.path}</span>
            {change.purpose ? <span className="text-muted-foreground"> — {change.purpose}</span> : null}
          </li>
        ))}
      </ul>
    </DetailRow>
  );
}

function StoryDetailBody({ projectId, story, activityEnabled }: { projectId: number; story: Story; activityEnabled: boolean }) {
  return (
    <>
      <DetailRow label="Ticket">{story.external_id || story.jira_key}</DetailRow>
      <ExternalChangePanel projectId={projectId} story={story} />
      <ClarificationPanel projectId={projectId} story={story} />
      <DetailRow label="Summary">{story.summary || "—"}</DetailRow>
      {story.description ? <DescriptionRow text={story.description} /> : null}
      <StoryPlanPanel storyKey={story.jira_key} open={activityEnabled} />
      <ReadOnlyChangesRow story={story} />
      <ReplanRow projectId={projectId} story={story} />
      <DetailRow label="Status">
        <StatusTagFor status={story.status} />
      </DetailRow>
      {story.external_status ? (
        <DetailRow label="Status in source">
          {story.external_closed
            ? `${story.external_status} — closed${story.status === "skipped" ? ", so forge skipped this story" : "; this run was already under way and will finish"}`
            : story.parked_reason === "on_hold_in_source"
              ? `${story.external_status} — forge leaves this ticket alone until it moves out of that status`
              : story.external_status}
        </DetailRow>
      ) : null}
      <DetailRow label="Kind">
        <KindDetailValue projectId={projectId} story={story} />
      </DetailRow>
      <DetailRow label="Parent story">{story.parent_key || "— (top-level)"}</DetailRow>
      <DetailRow label="Wave">{`W${story.wave}`}</DetailRow>
      {story.parked_reason ? <DetailRow label="Parked reason">{story.parked_reason}</DetailRow> : null}
      {story.parked_reason === "usage_limit" ? (
        <DetailRow label="Provider limit">
          {story.retry_at
            ? `The provider said the limit lifts at ${new Date(story.retry_at).toLocaleString()}; forge retries this story then. The story stays failed until it does.`
            : "The provider did not say when the limit lifts, so nothing is scheduled. Retry once it has (or add credit)."}
        </DetailRow>
      ) : null}
      <DetailRow label="Branch">{story.branch}</DetailRow>
      <PriorWorkRow prior={story.prior_work} />
      <DetailRow label="Gate approved">{story.gate_approved ? "Yes" : "No"}</DetailRow>
      {story.coverage_gap ? (
        <DetailRow label="Coverage gap">{story.coverage_gap_detail || "Tests passed; coverage threshold not met."}</DetailRow>
      ) : null}
      {story.pr_open_error ? <DetailRow label="PR not opened">{story.pr_open_error}</DetailRow> : null}
      {story.status === "failed" && story.failure_stage === "run" ? <RunOutcomeRow story={story} /> : null}
      {story.retry_count ? <DetailRow label="Retries">{String(story.retry_count)}</DetailRow> : null}
      {story.pull_request && story.pull_request.number ? (
        <DetailRow label="Pull request">
          <a className="text-primary underline underline-offset-2" href={story.pull_request.url || "#"} target="_blank" rel="noopener noreferrer">
            #{story.pull_request.number} ({story.pull_request.state})
          </a>
        </DetailRow>
      ) : null}
      <StoryUsagePanel storyKey={story.jira_key} />
      {story.status === "dispatched" && story.kind !== "qa" ? <GuidanceForm storyKey={story.jira_key} /> : null}
      <ActivityFeed storyKey={story.jira_key} enabled={activityEnabled} />
    </>
  );
}

export function DetailDialog({ projectId, epics, target, onClose, jiraBaseUrl, ticketSource }: DetailDialogProps) {
  const syncEpicFields = useSyncEpicFields(projectId);
  const syncStory = useSyncStory(projectId);

  const epic = target?.kind === "epic" ? epics.find((e) => e.jira_key === target.key) : undefined;
  const story =
    target?.kind === "story" ? epics.flatMap((e) => e.stories || []).find((s) => s.jira_key === target.key) : undefined;

  const open = target != null && (epic != null || story != null);
  const syncing = syncEpicFields.isPending || syncStory.isPending;
  // The ticket's own record says which system it came from; the project's
  // setting only decides for a ticket created before that was recorded.
  const linked = epic ?? story;
  const linkedSource = linked?.external_source ?? ticketSource;
  const shownKey = linked ? displayKey(linked) : (target?.key ?? "");
  const heading = target?.kind === "epic" ? `Task ${shownKey}` : `Story ${shownKey}`;
  const sourceUrl = target ? ticketUrl(linkedSource, linked?.external_id, target.key, jiraBaseUrl) : null;

  function handleSync() {
    if (!target) return;
    if (target.kind === "epic") {
      syncEpicFields.mutate(target.key);
    } else {
      syncStory.mutate(target.key);
    }
  }

  return (
    // Clearing the target unmounts the body (Dialog only renders its
    // content while `open` is true), which is what closes the activity
    // stream (ActivityFeed's effect cleanup) — miss this and the dialog
    // looks shut while its EventSource stays open for the life of the page.
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{heading}</DialogTitle>
        </DialogHeader>
        {/* min-w-0: DialogContent is a CSS grid, and a grid item's default
            automatic minimum width is its content's max-content size — an
            unwrapped plan_markdown paragraph (one long line, no hard wraps)
            would otherwise blow this out to thousands of pixels wide
            instead of wrapping inside the dialog. */}
        <div className="flex min-w-0 flex-col gap-3">
          {epic ? <EpicDetailBody epic={epic} /> : null}
          {story ? <StoryDetailBody projectId={projectId} story={story} activityEnabled={open} /> : null}
        </div>
        <DialogFooter className="sm:justify-between">
          <div className="flex gap-2">
            <Button variant="outline" loading={syncing} onClick={handleSync}>
              Sync from {sourceLabel(linkedSource)}
            </Button>
            <Button variant="outline" onClick={onClose}>
              Close
            </Button>
          </div>
          {sourceUrl ? (
            <Button asChild>
              <a href={sourceUrl} target="_blank" rel="noreferrer">
                Open in {sourceLabel(linkedSource)}
                <SquareArrowOutUpRight />
              </a>
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
