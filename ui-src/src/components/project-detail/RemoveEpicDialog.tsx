import { useRemoveEpic } from "../../hooks/useEpics";
import { useTicketFilter } from "../../hooks/useTicketFilter";
import { useToast } from "../../lib/toast";
import { displayKey, sourceLabel } from "../../lib/ticketSource";
import type { Epic } from "../../types/api";
import { ConfirmDialog } from "../ui/confirm-dialog";

// Removing a ticket forgets it in elstin — its board entry, run history, token
// usage, worktrees, local branches and saved run state — and nothing else.
// The confirmation spells out both halves, because "remove" is easy to read as
// "close": a pull request that was opened stays open, and a branch that was
// pushed stays pushed.
//
// Controlled: the epic row's ⋯ menu opens it. A ticket with a run in progress
// is refused by the server; `epicRemovable` says so before anyone asks.
export function epicRemovable(epic: Epic): boolean {
  return !(epic.stories ?? []).some((s) => s.status === "dispatched");
}

export function RemoveEpicDialog({
  projectId,
  epic,
  open,
  onOpenChange,
}: {
  projectId: number;
  epic: Epic;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const setOpen = onOpenChange;
  const removeEpic = useRemoveEpic(projectId);
  const filter = useTicketFilter(projectId);
  const { showToast } = useToast();

  const stories = epic.stories ?? [];
  const taskCount = stories.filter((s) => !s.parent_key).length;
  // The epic's own pull request, plus any a story opened before stories
  // merged locally — removal closes none of them.
  const pullRequests = [
    ...(epic.pull_requests ?? []),
    ...stories.map((s) => s.pull_request),
  ].filter((pr) => pr && pr.number);
  // A GitHub ticket lives on GitHub already; naming it twice would just be noise.
  const remote = epic.external_source === "github" ? "on GitHub" : `in ${sourceLabel(epic.external_source)} or on GitHub`;
  const label = displayKey(epic);

  function handleRemove() {
    removeEpic.mutate(epic.jira_key, {
      onSuccess: (report) => {
        setOpen(false);
        showToast(`Removed ${label} from Elstin`, "success");
        if (report.left_on_remote.pull_requests.length) {
          const numbers = report.left_on_remote.pull_requests.map((pr) => `#${pr.number}`).join(", ");
          showToast(`Pull request ${numbers} on GitHub was not touched — close it there if you want it gone.`, "info");
        }
        if (report.warnings.length) {
          showToast(`Removed, but ${report.warnings.length} local clean-up step(s) had a problem: ${report.warnings[0]}`, "warning");
        }
      },
      onError: (err) => showToast(err.message || "Could not remove this ticket", "error"),
    });
  }

  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title={`Remove ${label} from Elstin?`}
      description="This can't be undone."
      confirmLabel="Remove"
      onConfirm={handleRemove}
      pending={removeEpic.isPending}
    >
      <div>
        <p className="font-medium">Deleted from Elstin</p>
        <p className="text-muted-foreground">
          {taskCount > 1 ? `The ticket and its ${taskCount} tasks` : "The ticket"}, along with run history and token usage,
          worktrees, local branches and saved run state.
        </p>
      </div>
      <div>
        <p className="font-medium">Left as it is</p>
        <p className="text-muted-foreground">
          Nothing {remote} is changed.
          {pullRequests.length
            ? ` Pull request ${pullRequests.map((pr) => `#${pr!.number}`).join(", ")} stays open, and any branch that was pushed stays pushed.`
            : " A branch that was pushed stays pushed."}
        </p>
      </div>
      {filter.data?.enabled ? (
        <p className="text-warning-foreground bg-warning rounded-md px-3 py-2">
          This project's ticket filter is on, so its next sweep may add this ticket back.
        </p>
      ) : null}
    </ConfirmDialog>
  );
}
