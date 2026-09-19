import type { ReactNode } from "react";
import { SquareArrowOutUpRight } from "lucide-react";
import { PhaseTagFor, StatusTagFor } from "../shared/StatusTag";
import { ActivityFeed } from "./ActivityFeed";
import { EpicUsagePanel, StoryUsagePanel } from "./UsagePanel";
import { useResolveExternalChange, useSetStoryKind, useSyncStory } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { displayKey, sourceLabel, ticketUrl } from "../../lib/ticketSource";
import { useSyncEpicFields } from "../../hooks/useEpics";
import type { Epic, ExternalChangeAction, Story } from "../../types/api";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

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
      <DetailRow label="Waves">{(epic.levels || []).length ? String(epic.levels.length) : "0"}</DetailRow>
      <DetailRow label="Stories">{String((epic.stories || []).length)}</DetailRow>
      <EpicUsagePanel epicKey={epic.jira_key} />
    </>
  );
}

function StoryDetailBody({ projectId, story, activityEnabled }: { projectId: number; story: Story; activityEnabled: boolean }) {
  return (
    <>
      <DetailRow label="Ticket">{story.external_id || story.jira_key}</DetailRow>
      <ExternalChangePanel projectId={projectId} story={story} />
      <DetailRow label="Summary">{story.summary || "—"}</DetailRow>
      {story.description ? <DescriptionRow text={story.description} /> : null}
      <DetailRow label="Status">
        <StatusTagFor status={story.status} />
      </DetailRow>
      {story.external_status ? (
        <DetailRow label="Status in source">
          {story.external_closed
            ? `${story.external_status} — closed${story.status === "skipped" ? ", so forge skipped this story" : "; this run was already under way and will finish"}`
            : story.external_status}
        </DetailRow>
      ) : null}
      <DetailRow label="Kind">
        <KindDetailValue projectId={projectId} story={story} />
      </DetailRow>
      <DetailRow label="Parent story">{story.parent_key || "— (top-level)"}</DetailRow>
      <DetailRow label="Wave">{`W${story.wave}`}</DetailRow>
      {story.parked_reason ? <DetailRow label="Parked reason">{story.parked_reason}</DetailRow> : null}
      <DetailRow label="Branch">{story.branch}</DetailRow>
      <DetailRow label="Gate approved">{story.gate_approved ? "Yes" : "No"}</DetailRow>
      {story.coverage_gap ? (
        <DetailRow label="Coverage gap">{story.coverage_gap_detail || "Tests passed; coverage threshold not met."}</DetailRow>
      ) : null}
      {story.pr_open_error ? <DetailRow label="PR not opened">{story.pr_open_error}</DetailRow> : null}
      {story.retry_count ? <DetailRow label="Retries">{String(story.retry_count)}</DetailRow> : null}
      {story.pull_request && story.pull_request.number ? (
        <DetailRow label="Pull request">
          <a className="text-primary underline underline-offset-2" href={story.pull_request.url || "#"} target="_blank" rel="noopener noreferrer">
            #{story.pull_request.number} ({story.pull_request.state})
          </a>
        </DetailRow>
      ) : null}
      <StoryUsagePanel storyKey={story.jira_key} />
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
        <div className="flex flex-col gap-3">
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
