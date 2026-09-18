import { useState } from "react";
import { SettingsField } from "../settings/SettingsField";
import { useClearProjectConfig, useProjectConfig, useSetProjectConfig } from "../../hooks/useProjectConfig";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

interface ProjectSettingsDialogProps {
  projectId: number;
  open: boolean;
  onClose: () => void;
}

export function ProjectSettingsDialog({ projectId, open, onClose }: ProjectSettingsDialogProps) {
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
          <form className="flex flex-col gap-4" onSubmit={(e) => e.preventDefault()}>
            {(configQuery.data ?? []).map((field) => (
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
