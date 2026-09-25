import { useState } from "react";
import { useCancelStory } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

// Stops a story's run in flight. There used to be no way to: Pause only
// stops the next dispatch, so the only way to end a run that had gone wrong was
// to restart the whole stack. Cancelling leaves the story failed ("cancelled"),
// which is what makes Retry (start over) and Remove available.
export function CancelRunButton({ projectId, storyKey, label }: { projectId: number; storyKey: string; label: string }) {
  const [open, setOpen] = useState(false);
  const cancelStory = useCancelStory(projectId);
  const { showToast } = useToast();

  function handleCancel() {
    cancelStory.mutate(storyKey, {
      onSuccess: () => {
        setOpen(false);
        showToast(`Cancelled the run for ${label}`, "success");
      },
      onError: (err) => {
        setOpen(false);
        showToast(err.message || "Could not cancel this run", "error");
      },
    });
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className="shrink-0"
        title="Stop this run now"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        Cancel run
      </Button>
      <Dialog open={open} onOpenChange={(next) => !cancelStory.isPending && setOpen(next)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Cancel the run for {label}?</DialogTitle>
            <DialogDescription>The agents stop now and the story is marked failed (cancelled).</DialogDescription>
          </DialogHeader>
          <p className="text-muted-foreground text-sm">
            Work in its worktree is kept until you retry, and Retry starts over from planning. Model calls already made are still billed. Once
            it is cancelled you can also remove the ticket.
          </p>
          <DialogFooter>
            <Button variant="outline" disabled={cancelStory.isPending} onClick={() => setOpen(false)}>
              Keep running
            </Button>
            <Button variant="destructive" loading={cancelStory.isPending} onClick={handleCancel}>
              Cancel run
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
