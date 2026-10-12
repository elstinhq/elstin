import { useEffect, useState } from "react";
import { useProject } from "../../../hooks/useProjects";
import { usePoModePreview, useStartPoSession } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import type { PoMode } from "../../../types/api";
import { Link, navigate, useLocation, withSearch } from "../../../lib/router";
import { cn } from "../../../lib/utils";
import { Page } from "../../shell/Page";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { Label } from "../../ui/label";
import { Textarea } from "../../ui/textarea";

// What the session is for: a backlog (the mode then follows from the epic) or one bug.
// Which one is the person's call — nothing about a brief says it reports a fault — and
// it is kept in the address (?kind=bug) so "Report a bug" can link straight here.
type Kind = "backlog" | "bug";

const KINDS: { kind: Kind; label: string; note: string }[] = [
  { kind: "backlog", label: "Backlog", note: "Epics, stories and tasks" },
  { kind: "bug", label: "Bug", note: "One ticket, its cause traced in the code" },
];

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

// Starting a PO session (/projects/<id>/backlog/new, ?kind=bug for a bug). It ends on
// the new session's page, where the draft and its chat are worked.
export function PoStartPage({ projectId }: { projectId: number }) {
  const projectQuery = useProject(projectId);
  const [epicKey, setEpicKey] = useState("");
  const [jiraProject, setJiraProject] = useState("");
  const [brief, setBrief] = useState("");
  const [leaving, setLeaving] = useState(false);
  const kind: Kind = useLocation().search.get("kind") === "bug" ? "bug" : "backlog";
  const bug = kind === "bug";
  const start = useStartPoSession(projectId);
  const { showToast } = useToast();

  const projectHref = `/projects/${projectId}`;
  const backlogHref = `${projectHref}/backlog`;
  const typedKey = useDebounced(epicKey.trim(), 500);
  // A bug's epic is only where it is filed, so there is no mode to preview for it.
  const preview = usePoModePreview(projectId, bug ? "" : typedKey);
  const waiting = epicKey.trim() !== typedKey;

  function submit() {
    if (!brief.trim()) {
      showToast(bug ? "Describe the bug — what goes wrong, and where?" : "Write a brief — what is this backlog for?", "error");
      return;
    }
    start.mutate(
      {
        brief: brief.trim(),
        epic_key: epicKey.trim() || null,
        jira_project_key: jiraProject.trim() || null,
        mode: bug ? "bug" : null,
      },
      {
        onSuccess: (session) => {
          // Keep the button busy until the session's page replaces this one, so a
          // second click cannot start a second session.
          setLeaving(true);
          navigate(`${backlogHref}/${session.id}`);
        },
        onError: (err) => showToast(err.message || "Could not start the session", "error"),
      },
    );
  }

  return (
    <Page
      crumbs={[
        { label: projectQuery.data?.name ?? "Project", href: `${projectHref}/tasks` },
        { label: "Backlog drafts", href: backlogHref },
        { label: bug ? "Report a bug" : "Plan backlog" },
      ]}
    >
      <div className="mx-auto flex max-w-2xl flex-col gap-6">
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold tracking-tight">{bug ? "Report a bug" : "Plan backlog"}</h1>
          <p className="text-muted-foreground text-sm">
            {bug
              ? "The agent follows the report through the code to where it goes wrong, and drafts one Bug ticket: what happens, why, how sure it is, where the fix lies and what would confirm it. You edit it, talk it over, and publish it when it is right."
              : "The PO agent researches the code, Confluence and JIRA and drafts epics, stories and tasks. You edit the draft, talk it over with the agent, and publish it when it is right."}
          </p>
        </div>

        <div role="radiogroup" aria-label="What to draft" className="bg-muted grid grid-cols-2 gap-1 rounded-lg p-1">
          {KINDS.map((option) => (
            <button
              key={option.kind}
              type="button"
              role="radio"
              aria-checked={kind === option.kind}
              className={cn(
                "flex flex-col items-start rounded-md px-3 py-2 text-left transition-colors",
                kind === option.kind ? "bg-background shadow-xs" : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => navigate(withSearch({ kind: option.kind === "bug" ? "bug" : null }), { replace: true })}
            >
              <span className="text-sm font-medium">{option.label}</span>
              <span className="text-xs">{option.note}</span>
            </button>
          ))}
        </div>

        <form
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="po-epic-key">{bug ? "File it under an epic (optional)" : "Epic (optional)"}</Label>
            <Input
              id="po-epic-key"
              placeholder={bug ? "PROJ-123 — leave blank for a bug on its own" : "PROJ-123 — leave blank to draft a new epic"}
              value={epicKey}
              onChange={(e) => setEpicKey(e.target.value)}
            />
            {bug ? (
              <p className="text-muted-foreground text-xs">
                {epicKey.trim()
                  ? `The bug is created under ${epicKey.trim()}, and its stories are read for context.`
                  : "The bug becomes a ticket of its own, which Elstin can pick up and fix once you resume it."}
              </p>
            ) : (
              <ModeNote epicKey={epicKey.trim()} waiting={waiting} preview={preview} />
            )}
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="po-brief">{bug ? "What goes wrong" : "Brief"}</Label>
            <Textarea
              id="po-brief"
              className="min-h-48"
              placeholder={
                bug
                  ? "What happens, where (screen, report, endpoint), and what should happen instead. Paste the report, with an example if there is one."
                  : "What should be built, for whom, and why? Link any Confluence pages that matter."
              }
              value={brief}
              onChange={(e) => setBrief(e.target.value)}
            />
          </div>

          {!epicKey.trim() ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="po-jira-project">{bug ? "JIRA project for the bug (optional)" : "JIRA project for the new epic (optional)"}</Label>
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
              <Link href={backlogHref}>Cancel</Link>
            </Button>
            <Button type="submit" loading={start.isPending || leaving}>
              {bug ? "Start investigating" : "Start drafting"}
            </Button>
          </div>
        </form>
      </div>
    </Page>
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
