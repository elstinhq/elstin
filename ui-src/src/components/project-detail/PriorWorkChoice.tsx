import { useState } from "react";
import { usePickPriorBranch } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { displayKey } from "../../lib/ticketSource";
import { needsChoice } from "../../lib/priorWork";
import type { PriorWorkCandidate, Story } from "../../types/api";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

// Several branches carry diverged work for this ticket and none contains the
// others (forge/services/prior_work.py). forge does not guess which code to
// build on, so the story waits here — only it and the stories depending on
// it. Picking one makes forge's branch start from it; "Start fresh" starts
// from the epic branch as for any ticket. The branch itself is never changed.
export function PriorWorkChoiceButton({ projectId, story }: { projectId: number; story: Story }) {
  const [open, setOpen] = useState(false);
  const labels = needsChoice(story.prior_work);
  const label = labels[0];
  // A ticket closed as done is picked for too: its branch is merged into the epic branch.
  if ((story.status !== "pending" && story.status !== "skipped") || story.branch || label === undefined) return null;
  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        Choose branch
      </Button>
      {open ? (
        <PriorWorkChoiceDialog
          projectId={projectId}
          story={story}
          label={label}
          remaining={labels.length - 1}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function PriorWorkChoiceDialog({
  projectId,
  story,
  label,
  remaining,
  onClose,
}: {
  projectId: number;
  story: Story;
  label: string;
  // Repos still to decide after this one.
  remaining: number;
  onClose: () => void;
}) {
  const pick = usePickPriorBranch(projectId);
  const { showToast } = useToast();
  const finding = story.prior_work?.repos?.[label];
  const [selected, setSelected] = useState<string | null>(null);
  if (!finding) return null;
  const unmerged = finding.candidates.filter((c) => !c.in_default);

  function submit(branch: string | null) {
    pick.mutate(
      { storyKey: story.jira_key, repoLabel: label, branch },
      {
        onSuccess: () => {
          showToast(branch ? `${displayKey(story)} will continue from ${branch}` : `${displayKey(story)} will start fresh`, "success");
          onClose();
        },
        onError: (err) => showToast(err.message || "Could not record the choice", "error"),
      },
    );
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !pick.isPending && onClose()}>
      <DialogContent className="sm:max-w-xl" onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Which work should {displayKey(story)} continue?</DialogTitle>
          <DialogDescription>
            In {label}, {unmerged.length} branches carry work for this ticket that isn't in {finding.default_branch}, and none
            contains the others. forge starts its own branch from the one you pick and never changes the original.
            {remaining > 0 ? ` (${remaining} more repo${remaining > 1 ? "s" : ""} to decide after this one.)` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="flex max-h-[50vh] flex-col gap-2 overflow-y-auto" role="radiogroup" aria-label="Branches">
          {unmerged.map((candidate) => (
            <CandidateOption
              key={candidate.branch}
              candidate={candidate}
              selected={selected === candidate.branch}
              onSelect={() => setSelected(candidate.branch)}
            />
          ))}
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={pick.isPending} onClick={() => submit(null)}>
            Start fresh
          </Button>
          <Button disabled={!selected} loading={pick.isPending} onClick={() => selected && submit(selected)}>
            Continue from this branch
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function CandidateOption({ candidate, selected, onSelect }: { candidate: PriorWorkCandidate; selected: boolean; onSelect: () => void }) {
  const when = candidate.last_commit_at ? new Date(candidate.last_commit_at).toLocaleDateString() : null;
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
      className={`rounded-md border p-3 text-left text-sm transition-colors ${
        selected ? "border-primary bg-accent" : "border-border hover:bg-accent/50"
      }`}
    >
      <div className="font-mono text-xs font-medium">{candidate.branch}</div>
      <div className="text-muted-foreground mt-1 text-xs">
        {candidate.ahead} commit{candidate.ahead === 1 ? "" : "s"} ahead · {candidate.files_total} file
        {candidate.files_total === 1 ? "" : "s"}
        {candidate.last_author ? ` · last by ${candidate.last_author}` : ""}
        {when ? ` on ${when}` : ""}
        {candidate.unpushed ? " · has commits not pushed" : ""}
        {candidate.pull_request ? ` · PR #${candidate.pull_request.number} (${candidate.pull_request.state})` : ""}
      </div>
      {candidate.commits.length ? (
        <ul className="text-muted-foreground mt-2 list-none space-y-0.5 font-mono text-[11px]">
          {candidate.commits.slice(0, 5).map((commit) => (
            <li key={commit} className="truncate">
              {commit}
            </li>
          ))}
        </ul>
      ) : null}
    </button>
  );
}
