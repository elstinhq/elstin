import { useMemo, useState } from "react";
import { SettingsSection } from "../settings/SettingsSection";
import { DeleteProjectSection } from "./DeleteProjectSection";
import { useClearProjectConfig, useConfigCategories, useProjectConfig, useSetProjectConfig } from "../../hooks/useProjectConfig";
import { useSetTicketSource } from "../../hooks/useProjects";
import { buildSections } from "../../lib/settingsSections";
import { TICKET_SOURCES } from "../../lib/ticketSource";
import { useToast } from "../../lib/toast";
import { Alert, AlertDescription } from "../ui/alert";
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
  const categoriesQuery = useConfigCategories();
  const setConfig = useSetProjectConfig(projectId);
  const clearConfig = useClearProjectConfig(projectId);
  const { showToast } = useToast();
  const [savingKey, setSavingKey] = useState<string | null>(null);
  // null until a person opens or closes one: then the sections that hold an
  // override start open, so what this project customizes is visible at once.
  const [openIds, setOpenIds] = useState<Set<string> | null>(null);

  // A GitHub project has no use for the JIRA connection fields.
  const sections = useMemo(
    () =>
      buildSections(
        (configQuery.data ?? []).filter((field) => !(ticketSource === "github" && field.key.startsWith("jira_"))),
        categoriesQuery.data,
      ),
    [configQuery.data, categoriesQuery.data, ticketSource],
  );
  const withOverrides = useMemo(
    () => new Set(sections.filter((s) => s.fields.some((f) => f.overridden)).map((s) => s.id)),
    [sections],
  );
  const openSections = openIds ?? withOverrides;

  // A project that sends its calls to its own endpoint must bring its own key —
  // the global key is never sent to an endpoint it was not issued for
  // (forge/llm/registry.py) — so say so here, not when its first run fails.
  const byKey = new Map((configQuery.data ?? []).map((field) => [field.key, field]));
  const own = (key: string) => (byKey.get(key)?.overridden ? (byKey.get(key)?.value ?? "").trim() : "");
  const ownsEndpoint = own("llm_base_url") !== "" || (own("llm_provider") !== "" && own("llm_provider").toLowerCase() !== "openai");
  const ownsKey = Boolean(byKey.get("openai_api_key")?.overridden && byKey.get("openai_api_key")?.is_set);

  function setSectionOpen(id: string, next: boolean) {
    setOpenIds((current) => {
      const ids = new Set(current ?? withOverrides);
      if (next) ids.add(id);
      else ids.delete(id);
      return ids;
    });
  }

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
      <DialogContent className="sm:max-w-2xl">
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
          {ownsEndpoint && !ownsKey ? (
            <Alert variant="destructive">
              <AlertDescription>
                This project sends its model calls to its own endpoint but has no API key of its own, so its runs will
                stop until you set one under Model provider. The global key is never sent to another endpoint.
              </AlertDescription>
            </Alert>
          ) : null}
          <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
            {sections.map((section) => (
              <SettingsSection
                key={section.id}
                section={section}
                open={openSections.has(section.id)}
                onOpenChange={(next) => setSectionOpen(section.id, next)}
                savingKey={savingKey}
                onSave={handleSave}
                projectId={projectId}
                overrideFor={(field) => ({ onReset: () => clearConfig.mutate(field.key) })}
              />
            ))}
          </form>
          <DeleteProjectSection projectId={projectId} />
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
