import { useState } from "react";
import { SettingsField } from "../settings/SettingsField";
import { useClearProjectConfig, useProjectConfig, useSetProjectConfig } from "../../hooks/useProjectConfig";
import { useSetTicketSource } from "../../hooks/useProjects";
import { TICKET_SOURCES } from "../../lib/ticketSource";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Label } from "../ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";

interface ProjectSettingsDialogProps {
  projectId: number;
  ticketSource: string;
  open: boolean;
  onClose: () => void;
}

export function ProjectSettingsDialog({ projectId, ticketSource, open, onClose }: ProjectSettingsDialogProps) {
  const setSource = useSetTicketSource(projectId);
  const configQuery = useProjectConfig(projectId);
  const setConfig = useSetProjectConfig(projectId);
  const clearConfig = useClearProjectConfig(projectId);
  const { showToast } = useToast();
  const [savingKey, setSavingKey] = useState<string | null>(null);

  function handleSave(key: string, value: string) {
    setSavingKey(key);
    setConfig.mutate(
      { key, value },
      {
        onSuccess: () => showToast("Saved", "success"),
        onError: () => showToast("Save failed", "error"),
        onSettled: () => setSavingKey(null),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Settings</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <p className="text-muted-foreground text-xs">
            Anything not overridden here uses the{" "}
            <a className="text-primary underline underline-offset-2" href="/settings">
              global default
            </a>
            .
          </p>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="ticket-source">Ticket source</Label>
            <Select
              value={ticketSource}
              onValueChange={(next) =>
                setSource.mutate(next, {
                  onSuccess: () => showToast("Ticket source changed — tickets already on the board keep the source they came from", "success"),
                  onError: (err) => showToast(err.message || "Could not change the ticket source", "error"),
                })
              }
            >
              <SelectTrigger id="ticket-source" className="w-full" disabled={setSource.isPending}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TICKET_SOURCES.map((s) => (
                  <SelectItem key={s.value} value={s.value}>
                    {s.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-muted-foreground text-xs">
              Where "Add ticket" and the ticket filter look. GitHub Issues uses the GitHub token from global settings.
            </p>
          </div>
          <form className="flex flex-col gap-4" onSubmit={(e) => e.preventDefault()}>
            {/* A GitHub project has no use for the JIRA connection fields. */}
            {(configQuery.data ?? [])
              .filter((field) => !(ticketSource === "github" && field.key.startsWith("jira_")))
              .map((field) => (
              <SettingsField
                key={field.key}
                field={field}
                saving={savingKey === field.key}
                onSave={handleSave}
                override={{ onReset: () => clearConfig.mutate(field.key) }}
              />
            ))}
          </form>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
