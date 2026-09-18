import { useEffect, useState } from "react";
import {
  useClearTicketFilter,
  usePreviewTicketFilter,
  useRunTicketFilterSync,
  useSaveTicketFilter,
  useTicketFilter,
} from "../../hooks/useTicketFilter";
import { useToast } from "../../lib/toast";
import type {
  TicketFilter,
  TicketFilterInput,
  TicketFilterMatch,
  TicketFilterSyncResult,
} from "../../types/api";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Textarea } from "../ui/textarea";

interface TicketFilterDialogProps {
  projectId: number;
  open: boolean;
  onClose: () => void;
}

/** The dialog's own state. The multi-value fields are held as raw text,
 *  not as arrays, because splitting on every keystroke and re-joining for
 *  the input's value eats the comma the moment it is typed. They become
 *  arrays once, on submit. */
interface Draft {
  enabled: boolean;
  jira_project_key: string;
  assignee: string;
  statuses: string;
  labels: string;
  issue_types: string;
  extra_jql: string;
}

const EMPTY: Draft = {
  enabled: false,
  jira_project_key: "",
  assignee: "",
  statuses: "",
  labels: "",
  issue_types: "",
  extra_jql: "",
};

// Comma-separated text rather than pickers: valid statuses/labels/types are
// per-JIRA-instance and forge has no schema endpoint to populate a dropdown
// from, so a picker would either be wrong or need a round-trip per field
// just to open the dialog.
const splitList = (raw: string): string[] =>
  raw
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);

function toDraft(saved: TicketFilter | null | undefined): Draft {
  if (!saved) return EMPTY;
  return {
    enabled: saved.enabled,
    jira_project_key: saved.jira_project_key ?? "",
    assignee: saved.assignee ?? "",
    statuses: saved.statuses.join(", "),
    labels: saved.labels.join(", "),
    issue_types: saved.issue_types.join(", "),
    extra_jql: saved.extra_jql ?? "",
  };
}

function toInput(draft: Draft): TicketFilterInput {
  return {
    enabled: draft.enabled,
    jira_project_key: draft.jira_project_key.trim() || null,
    assignee: draft.assignee.trim() || null,
    statuses: splitList(draft.statuses),
    labels: splitList(draft.labels),
    issue_types: splitList(draft.issue_types),
    extra_jql: draft.extra_jql.trim() || null,
  };
}

function summariseSync(result: TicketFilterSyncResult): string {
  // "Nothing matched" and "everything that matched was already here" are
  // different answers, and the second one is the common steady state once a
  // filter has been running — reporting both as "no new tickets" made a
  // working sweep look like a broken one.
  const parts: string[] = [];
  if (result.synced.length) parts.push(`added ${result.synced.length} ticket(s)`);
  if (result.refreshed.length) parts.push(`picked up new stories under ${result.refreshed.join(", ")}`);
  if (!parts.length) return result.matched ? "Already up to date" : "Nothing matched the filter";
  return parts.join(" · ");
}

function MatchRow({ match }: { match: TicketFilterMatch }) {
  return (
    <div className="flex flex-col gap-0.5 py-1">
      <span className="text-sm font-medium">
        {match.key} — {match.summary}
      </span>
      <span className="text-muted-foreground text-xs">
        {match.issue_type} · {match.status} ·{" "}
        {match.action === "sync"
          ? "will be added"
          : match.action === "refresh"
            ? "already added — will be re-checked for new child tickets"
            : `skipped: ${match.reason}`}
      </span>
    </div>
  );
}

export function TicketFilterDialog({ projectId, open, onClose }: TicketFilterDialogProps) {
  const filterQuery = useTicketFilter(projectId);
  const saveFilter = useSaveTicketFilter(projectId);
  const clearFilter = useClearTicketFilter(projectId);
  const previewFilter = usePreviewTicketFilter(projectId);
  const runSync = useRunTicketFilterSync(projectId);
  const { showToast } = useToast();

  const [draft, setDraft] = useState<Draft>(EMPTY);
  // Hydrate the draft from the server exactly once per opening — not on
  // every change to the query data. A save or a sweep invalidates this
  // query, and re-seeding on that would both discard an in-progress edit
  // and wipe the result panel the user just asked for.
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    if (!open) {
      setHydrated(false);
      return;
    }
    // A cancelled edit must not linger into the next visit.
    previewFilter.reset();
    runSync.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (open && !hydrated && filterQuery.isSuccess) {
      setDraft(toDraft(filterQuery.data));
      setHydrated(true);
    }
  }, [open, hydrated, filterQuery.isSuccess, filterQuery.data]);

  const saved = filterQuery.data ?? null;
  const patch = (fields: Partial<Draft>) => setDraft((current) => ({ ...current, ...fields }));

  function handleSave() {
    saveFilter.mutate(toInput(draft), {
      onSuccess: () => {
        showToast("Filter saved", "success");
        onClose();
      },
      onError: (err) => showToast(err.message || "Could not save the filter", "error"),
    });
  }

  function handlePreview() {
    previewFilter.mutate(toInput(draft), {
      onError: (err) => showToast(err.message || "Preview failed", "error"),
    });
  }

  function handleSyncNow() {
    // Sweeps the draft, not the saved filter — otherwise "Sync now" after an
    // edit would silently run the old query.
    runSync.mutate(toInput(draft), {
      onSuccess: (result) => showToast(summariseSync(result), "success"),
      onError: (err) => showToast(err.message || "Sync failed", "error"),
    });
  }

  function handleRemove() {
    clearFilter.mutate(undefined, {
      onSuccess: () => {
        setDraft(EMPTY);
        showToast("Filter removed", "success");
      },
      onError: (err) => showToast(err.message || "Could not remove the filter", "error"),
    });
  }

  const preview = previewFilter.data;
  const syncResult = runSync.data;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Ticket filter</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <p className="text-muted-foreground text-xs">
            Pull tickets in from a saved JIRA query instead of adding them one key at a time. New matches are added paused
            and without a folder — pick a folder and resume to start work. Tickets that stop matching are never removed.
          </p>

          <div className="flex items-center justify-between gap-3">
            <Label htmlFor="filter-enabled" className="font-normal">
              Keep checking JIRA in the background
            </Label>
            <Switch id="filter-enabled" checked={draft.enabled} onCheckedChange={(checked) => patch({ enabled: checked })} />
          </div>

          <form className="flex flex-col gap-4" onSubmit={(e) => e.preventDefault()}>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-project-key">JIRA project key</Label>
              <Input
                id="filter-project-key"
                placeholder="ENG"
                value={draft.jira_project_key}
                onChange={(e) => patch({ jira_project_key: e.target.value })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-assignee">Assignee</Label>
              <Input
                id="filter-assignee"
                placeholder="currentUser()"
                value={draft.assignee}
                onChange={(e) => patch({ assignee: e.target.value })}
              />
              <p className="text-muted-foreground text-xs">An account ID, an email, or currentUser()</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-statuses">Statuses</Label>
              <Input
                id="filter-statuses"
                placeholder="To Do, In Progress"
                value={draft.statuses}
                onChange={(e) => patch({ statuses: e.target.value })}
              />
              <p className="text-muted-foreground text-xs">Comma-separated</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-labels">Labels</Label>
              <Input
                id="filter-labels"
                placeholder="forge"
                value={draft.labels}
                onChange={(e) => patch({ labels: e.target.value })}
              />
              <p className="text-muted-foreground text-xs">Comma-separated</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-issue-types">Issue types</Label>
              <Input
                id="filter-issue-types"
                placeholder="Story, Bug"
                value={draft.issue_types}
                onChange={(e) => patch({ issue_types: e.target.value })}
              />
              <p className="text-muted-foreground text-xs">Comma-separated</p>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="filter-jql">Extra JQL</Label>
              <Textarea
                id="filter-jql"
                rows={2}
                placeholder="sprint in openSprints()"
                value={draft.extra_jql}
                onChange={(e) => patch({ extra_jql: e.target.value })}
              />
              <p className="text-muted-foreground text-xs">Optional — AND-ed with the fields above, for anything they can't express</p>
            </div>
          </form>

          {saved?.jql ? (
            <div className="flex flex-col gap-1">
              <span className="text-muted-foreground text-xs">Saved query</span>
              <code className="text-xs break-words">{saved.jql}</code>
            </div>
          ) : null}

          {saved?.last_synced_at ? (
            <p className="text-muted-foreground text-xs">
              Last run {new Date(saved.last_synced_at).toLocaleString()} — {saved.last_matched_count ?? 0} matched,{" "}
              {saved.last_synced_count ?? 0} added
            </p>
          ) : null}

          {saved?.last_error ? (
            <Alert variant="destructive">
              <AlertDescription>{saved.last_error}</AlertDescription>
            </Alert>
          ) : null}

          <div className="flex flex-wrap gap-3">
            <Button variant="outline" size="sm" loading={previewFilter.isPending} onClick={handlePreview}>
              Test filter
            </Button>
            <Button variant="outline" size="sm" loading={runSync.isPending} onClick={handleSyncNow}>
              Sync now
            </Button>
            {saved ? (
              <Button variant="destructive" size="sm" loading={clearFilter.isPending} onClick={handleRemove}>
                Remove filter
              </Button>
            ) : null}
          </div>

          {preview ? (
            <div className="flex flex-col gap-2">
              <span className="text-sm font-bold">
                {preview.matched} matched · {preview.would_sync.length} would be added
                {preview.would_refresh.length ? ` · ${preview.would_refresh.length} re-checked` : ""}
                {preview.truncated ? " (result capped — narrow the filter)" : ""}
              </span>
              <code className="text-xs break-words">{preview.jql}</code>
              {preview.matches.map((match) => (
                <MatchRow key={match.key} match={match} />
              ))}
            </div>
          ) : null}

          {syncResult ? (
            <div className="flex flex-col gap-2">
              <span className="text-sm font-bold">
                Added {syncResult.synced.length} of {syncResult.matched} matched
                {syncResult.refreshed.length ? ` · ${syncResult.refreshed.length} re-checked` : ""}
              </span>
              {syncResult.synced.length ? <span className="text-xs">{syncResult.synced.join(", ")}</span> : null}
              {syncResult.errors.map((error) => (
                <span key={error.key} className="text-destructive text-xs">
                  {error.key}: {error.error}
                </span>
              ))}
            </div>
          ) : null}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={saveFilter.isPending} onClick={handleSave}>
            Save filter
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
