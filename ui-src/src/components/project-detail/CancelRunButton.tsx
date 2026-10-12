import { useState } from "react";
import { useCancelStory } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { ConfirmDialog } from "../ui/confirm-dialog";

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
        variant="link"
        size="sm"
        className="text-foreground h-auto shrink-0 px-0 underline"
        title="Stop this run now"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
      >
        Cancel run
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Cancel the run for ${label}?`}
        description="The agents stop now and the story is marked failed (cancelled)."
        confirmLabel="Cancel run"
        onConfirm={handleCancel}
        pending={cancelStory.isPending}
      >
        <p className="text-muted-foreground">
          Work in its worktree is kept until you retry, and Retry starts over from planning. Model calls already made are still billed. Once
          it is cancelled you can also remove the ticket.
        </p>
      </ConfirmDialog>
    </>
  );
}
