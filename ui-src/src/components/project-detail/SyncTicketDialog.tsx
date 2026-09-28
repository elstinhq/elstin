import { useEffect, useState } from "react";
import { useSyncEpic } from "../../hooks/useEpics";
import { useToast } from "../../lib/toast";
import { keyLabel, keyPlaceholder, sourceLabel } from "../../lib/ticketSource";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";

interface SyncTicketDialogProps {
  projectId: number;
  ticketSource: string;
  open: boolean;
  onClose: () => void;
}

export function SyncTicketDialog({ projectId, ticketSource, open, onClose }: SyncTicketDialogProps) {
  const [jiraKey, setJiraKey] = useState("");
  const syncEpic = useSyncEpic(projectId);
  const { showToast } = useToast();

  useEffect(() => {
    if (open) setJiraKey("");
  }, [open]);

  function handleSubmit() {
    const trimmed = jiraKey.trim();
    if (!trimmed) {
      showToast(`${keyLabel(ticketSource)} is required`, "error");
      return;
    }
    syncEpic.mutate(trimmed, {
      onSuccess: () => {
        showToast(`Synced ${trimmed}`, "success");
        onClose();
      },
      onError: (err) => showToast(err.message || "Sync failed", "error"),
    });
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add ticket</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <p className="text-muted-foreground text-xs">
            {ticketSource === "github"
              ? "Sync a GitHub issue with its sub-issues as stories, or a lone issue. "
              : "Sync a JIRA task with child stories, or a lone Story/Task ticket. "}
            New tickets start paused — resume when you're ready to run.
          </p>
          <form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault();
              handleSubmit();
            }}
          >
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="jira-key">{keyLabel(ticketSource)}</Label>
              <Input id="jira-key" placeholder={keyPlaceholder(ticketSource)} value={jiraKey} onChange={(e) => setJiraKey(e.target.value)} />
            </div>
          </form>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={syncEpic.isPending} onClick={handleSubmit}>
            Sync from {sourceLabel(ticketSource)}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
