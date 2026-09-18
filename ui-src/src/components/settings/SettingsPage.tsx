import { useState } from "react";
import { AppShell } from "../shared/AppShell";
import { SettingsField } from "./SettingsField";
import { useGlobalConfig, useSetGlobalConfig } from "../../hooks/useProjectConfig";
import { useToast } from "../../lib/toast";

export function SettingsPage() {
  const configQuery = useGlobalConfig();
  const setConfig = useSetGlobalConfig();
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
    <AppShell rightLinkHref="/projects" rightLinkLabel="Projects">
      <h1 className="text-xl font-semibold">Settings</h1>
      <p className="text-muted-foreground mt-1 text-sm">
        Everything here is stored in Postgres, not a file. No restart needed after saving.
      </p>
      <form className="mt-6 flex max-w-xl flex-col gap-4" onSubmit={(e) => e.preventDefault()}>
        {(configQuery.data ?? []).map((field) => (
          <SettingsField key={field.key} field={field} saving={savingKey === field.key} onSave={handleSave} />
        ))}
      </form>
    </AppShell>
  );
}
