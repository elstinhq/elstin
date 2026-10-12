import { RefreshCw } from "lucide-react";
import { useRefreshPriorWork } from "../../hooks/useEpics";
import { continuedBranches, doneWorkOutcome, lookupDue, needsChoice, onlyInDefault } from "../../lib/priorWork";
import { displayKey } from "../../lib/ticketSource";
import { useToast } from "../../lib/toast";
import type { Epic, Story } from "../../types/api";
import { Button } from "../ui/button";

// What the look-up of earlier work found (elstin/services/prior_work.py), in
// one line: which tickets continue an existing branch, which wait for a pick,
// which are already in main — and "Look again" while any ticket has not
// started, for a branch pushed after the epic was added.
export function PriorWorkSummary({ projectId, epic, stories }: { projectId: number; epic: Epic; stories: Story[] }) {
  const refresh = useRefreshPriorWork(projectId);
  const { showToast } = useToast();
  const unstarted = stories.filter((s) => s.status === "pending" && !s.branch);
  const lookups = [epic.prior_work, ...stories.map((s) => s.prior_work)].filter((p) => p !== null);
  // Nothing is looked up until the project has a folder to look in.
  if (!epic.repo || (!lookups.length && !unstarted.length)) return null;

  const continuing = stories
    .filter((s) => s.status !== "skipped" && continuedBranches(s.prior_work).length)
    .map((s) => displayKey(s));
  const doneMerged = stories.filter((s) => s.status === "skipped" && doneWorkOutcome(s.prior_work) === "merged").map((s) => displayKey(s));
  const doneConflicts = stories.filter((s) => s.status === "skipped" && doneWorkOutcome(s.prior_work) === "conflict").map((s) => displayKey(s));
  const choosing = stories
    .filter((s) => (s.status === "pending" || s.status === "skipped") && !s.branch && needsChoice(s.prior_work).length)
    .map((s) => displayKey(s));
  const inMain = stories.filter((s) => onlyInDefault(s.prior_work)).map((s) => displayKey(s));
  const epicBranch = continuedBranches(epic.prior_work)[0]?.candidate.branch;
  const pendingLookup = unstarted.some((s) => lookupDue(s.prior_work));

  const parts = [
    epicBranch ? `epic branch continues ${epicBranch}` : null,
    continuing.length ? `continuing existing work: ${continuing.join(", ")}` : null,
    doneMerged.length ? `done tickets' work merged in: ${doneMerged.join(", ")}` : null,
    doneConflicts.length ? `done work that conflicts: ${doneConflicts.join(", ")}` : null,
    choosing.length ? `pick a branch: ${choosing.join(", ")}` : null,
    inMain.length ? `already in main: ${inMain.join(", ")}` : null,
  ].filter(Boolean);
  const text = pendingLookup
    ? "Looking for earlier work on these tickets…"
    : parts.length
      ? `Earlier work — ${parts.join(" · ")}`
      : "No earlier work found for these tickets";

  return (
    <div className="text-muted-foreground flex min-w-0 items-center gap-2 text-xs">
      <span className="min-w-0 truncate" title={text}>{text}</span>
      {unstarted.length && !pendingLookup ? (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 shrink-0 px-2 text-xs"
          loading={refresh.isPending}
          title="Look for branches again — e.g. one pushed after this epic was added. Tickets already started keep the branch they started from."
          onClick={(e) => {
            e.stopPropagation();
            refresh.mutate(epic.jira_key, {
              onSuccess: () => showToast("Looking again on the next poll", "success"),
              onError: (err) => showToast(err.message || "Could not look again", "error"),
            });
          }}
        >
          <RefreshCw className="size-3" />
          Look again
        </Button>
      ) : null}
    </div>
  );
}
