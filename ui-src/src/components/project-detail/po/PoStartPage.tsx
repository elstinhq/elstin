import { useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useProject } from "../../../hooks/useProjects";
import { usePoModePreview, useStartPoSession } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import type { PoMode } from "../../../types/api";
import { AppShell } from "../../shared/AppShell";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { Label } from "../../ui/label";
import { Textarea } from "../../ui/textarea";

// What the agent will do, in words, for the mode elstin picked. The mode is
// chosen by the server from what the epic already has — never by the model, and
// not by this page: it only shows the choice before anything is started.
function modeNote(mode: PoMode, epicKey: string | null, stories: number): string {
  if (mode === "brainstorm") {
    return "New epic — the agent drafts an epic and its stories from your brief, reading the code, Confluence and JIRA first.";
  }
  if (mode === "split") {
    return `Split — ${epicKey} has no stories yet. The agent breaks it into stories and tasks, and may suggest a sharper description for the epic but never rewrites it.`;
  }
  return `Gap analysis — ${epicKey} already has ${stories} ${stories === 1 ? "story" : "stories"}. The agent assesses each against the code and JIRA, finds what nothing covers, and proposes only what is new. Existing tickets are never rewritten.`;
}

// A short pause after typing before the epic is looked up: each lookup is a
// round trip to the ticket source.
function useDebounced(value: string, ms: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms);
    return () => clearTimeout(timer);
  }, [value, ms]);
  return debounced;
}

// Starting a PO session, on its own page (/projects/<id>/po/new). It ends on the
// new session's page, where the draft and its chat are worked.
export function PoStartPage({ projectId }: { projectId: number }) {
  const projectQuery = useProject(projectId);
  const [epicKey, setEpicKey] = useState("");
  const [jiraProject, setJiraProject] = useState("");
  const [brief, setBrief] = useState("");
  const [leaving, setLeaving] = useState(false);
  const start = useStartPoSession(projectId);
  const { showToast } = useToast();

  const projectHref = `/projects/${projectId}`;
  const typedKey = useDebounced(epicKey.trim(), 500);
  const preview = usePoModePreview(projectId, typedKey);
  const waiting = epicKey.trim() !== typedKey;

  function submit() {
    if (!brief.trim()) {
      showToast("Write a brief — what is this backlog for?", "error");
      return;
    }
    start.mutate(
      { brief: brief.trim(), epic_key: epicKey.trim() || null, jira_project_key: jiraProject.trim() || null },
      {
        onSuccess: (session) => {
          // Keep the button busy through the page load, so a second click cannot start a second session.
          setLeaving(true);
          window.location.assign(`${projectHref}/po/${session.id}`);
        },
        onError: (err) => showToast(err.message || "Could not start the session", "error"),
      },
    );
  }

  if (projectQuery.isSuccess && !projectQuery.data) {
    return (
      <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
        <h1 className="text-xl font-semibold">Project not found</h1>
      </AppShell>
    );
  }

  return (
    <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
      <div className="mx-auto flex max-w-2xl flex-col gap-6">
        <div className="flex flex-col gap-3">
          <a href={projectHref} className="text-muted-foreground hover:text-foreground inline-flex w-fit items-center gap-1 text-sm">
            <ArrowLeft className="size-4" />
            {projectQuery.data?.name ?? "Project"}
          </a>
          <h1 className="text-xl font-semibold">Plan backlog</h1>
          <p className="text-muted-foreground text-sm">
            The PO agent researches the code, Confluence and JIRA and drafts epics, stories and tasks. You edit the draft, talk it over with the agent, and publish it when it is right.
          </p>
        </div>

        <form
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="po-epic-key">Epic (optional)</Label>
            <Input
              id="po-epic-key"
              placeholder="PROJ-123 — leave blank to draft a new epic"
              value={epicKey}
              onChange={(e) => setEpicKey(e.target.value)}
            />
            <ModeNote epicKey={epicKey.trim()} waiting={waiting} preview={preview} />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="po-brief">Brief</Label>
            <Textarea
              id="po-brief"
              className="min-h-48"
              placeholder="What should be built, for whom, and why? Link any Confluence pages that matter."
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
            />
          </div>

          {!epicKey.trim() ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="po-jira-project">JIRA project for the new epic (optional)</Label>
              <Input
                id="po-jira-project"
                placeholder="PROJ — defaults to the project's ticket filter"
                value={jiraProject}
                onChange={(e) => setJiraProject(e.target.value)}
              />
              <span className="text-muted-foreground text-xs">Only used when you publish; the draft itself does not need it.</span>
            </div>
          ) : null}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" asChild>
              <a href={projectHref}>Cancel</a>
            </Button>
            <Button type="submit" loading={start.isPending || leaving}>
              Start drafting
            </Button>
          </div>
        </form>
      </div>
    </AppShell>
  );
}

function ModeNote({
  epicKey,
  waiting,
  preview,
}: {
  epicKey: string;
  waiting: boolean;
  preview: ReturnType<typeof usePoModePreview>;
}) {
  if (!epicKey) return <p className="text-muted-foreground text-xs">{modeNote("brainstorm", null, 0)}</p>;
  if (waiting || preview.isFetching) return <p className="text-muted-foreground text-xs">Looking up {epicKey}…</p>;
  if (preview.isError) {
    return (
      <Alert variant="warning" className="text-foreground">
        <AlertDescription>{preview.error.message}</AlertDescription>
      </Alert>
    );
  }
  if (preview.data) {
    return (
      <p className="text-muted-foreground text-xs">
        {preview.data.epic_summary ? <strong className="text-foreground">{preview.data.epic_summary}. </strong> : null}
        {modeNote(preview.data.mode, preview.data.epic_key, preview.data.stories)}
      </p>
    );
  }
  return null;
}
