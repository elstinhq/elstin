import { useEffect, useMemo, useState } from "react";
import { Search } from "lucide-react";
import { AppShell } from "../shared/AppShell";
import { Alert, AlertDescription } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { SettingsSection } from "./SettingsSection";
import { useConfigCategories, useGlobalConfig, useSetGlobalConfig } from "../../hooks/useProjectConfig";
import { buildSections } from "../../lib/settingsSections";
import { useToast } from "../../lib/toast";

const STORAGE_KEY = "forge.settings.open";

// Which sections were open last time, plus the one a `#planner`-style link
// names. Storage can be unavailable (a private window, blocked site data) or
// hold junk; either way the page just starts collapsed.
function readOpenIds(): Set<string> {
  const ids = new Set<string>();
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    if (Array.isArray(saved)) for (const id of saved) if (typeof id === "string") ids.add(id);
  } catch {
    /* start collapsed */
  }
  const fromHash = window.location.hash.slice(1);
  if (fromHash) ids.add(fromHash);
  return ids;
}

function writeOpenIds(ids: Set<string>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...ids]));
  } catch {
    /* remembering is a convenience, not worth an error */
  }
}

export function SettingsPage() {
  const configQuery = useGlobalConfig();
  const categoriesQuery = useConfigCategories();
  const setConfig = useSetGlobalConfig();
  const { showToast } = useToast();
  const [savingKey, setSavingKey] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [openIds, setOpenIds] = useState<Set<string>>(readOpenIds);

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

  useEffect(() => writeOpenIds(openIds), [openIds]);

  function setOpen(id: string, open: boolean) {
    setOpenIds((current) => {
      const next = new Set(current);
      if (open) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  const loading = configQuery.isLoading || categoriesQuery.isLoading;
  // Underscores read as spaces on both sides, so "max turns" and "max_turns"
  // find the same setting whether a person knows its label or its key.
  const needle = query.trim().toLowerCase().replace(/_/g, " ");
  const filtering = needle !== "";
  // Without sections (their request failed) there is one "All settings" group,
  // and hiding it behind a collapsed header would hide the whole page.
  const sectionsMissing = categoriesQuery.isError;

  const sections = useMemo(
    () => (loading ? [] : buildSections(configQuery.data ?? [], categoriesQuery.data)),
    [loading, configQuery.data, categoriesQuery.data],
  );
  const visible = useMemo(
    () =>
      sections
        .map((section) => ({
          ...section,
          fields: filtering
            ? section.fields.filter((field) => `${field.label} ${field.key}`.toLowerCase().replace(/_/g, " ").includes(needle))
            : section.fields,
        }))
        .filter((section) => section.fields.length > 0),
    [sections, filtering, needle],
  );
  const matchCount = visible.reduce((total, section) => total + section.fields.length, 0);

  // A link to /settings#planner opens that section; bring it into view once
  // the sections exist to be found.
  useEffect(() => {
    const id = window.location.hash.slice(1);
    if (id && sections.some((section) => section.id === id)) {
      document.getElementById(id)?.scrollIntoView({ block: "start" });
    }
  }, [sections]);

  return (
    <AppShell rightLinkHref="/projects" rightLinkLabel="Projects">
      <div className="mx-auto w-full max-w-3xl">
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Stored in Postgres, not a file. Changes save as you leave a field and take effect without a restart.
        </p>

        <div className="mt-5 flex flex-wrap items-center gap-2">
          <div className="relative min-w-48 flex-1">
            <Search
              className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2"
              aria-hidden="true"
            />
            <Input
              type="search"
              aria-label="Filter settings"
              placeholder="Filter settings"
              className="pl-8"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          {!filtering && !sectionsMissing ? (
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="sm" onClick={() => setOpenIds(new Set(sections.map((section) => section.id)))}>
                Expand all
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setOpenIds(new Set())}>
                Collapse all
              </Button>
            </div>
          ) : null}
        </div>

        <p className="text-muted-foreground mt-2 min-h-5 text-xs" aria-live="polite">
          {filtering ? (matchCount === 1 ? "1 setting matches" : `${matchCount} settings match`) : ""}
        </p>

        {configQuery.isError ? (
          <Alert variant="destructive" className="mt-2">
            <AlertDescription>Could not load the settings. Reload the page to try again.</AlertDescription>
          </Alert>
        ) : loading ? (
          <p className="text-muted-foreground mt-4 text-sm">Loading settings…</p>
        ) : visible.length === 0 ? (
          <p className="text-muted-foreground mt-4 text-sm">No settings match “{query.trim()}”.</p>
        ) : (
          <form className="flex flex-col gap-3" onSubmit={(e) => e.preventDefault()}>
            {visible.map((section) => (
              <SettingsSection
                key={section.id}
                section={section}
                open={filtering || sectionsMissing || openIds.has(section.id)}
                onOpenChange={(open) => setOpen(section.id, open)}
                savingKey={savingKey}
                onSave={handleSave}
              />
            ))}
          </form>
        )}
      </div>
    </AppShell>
  );
}
