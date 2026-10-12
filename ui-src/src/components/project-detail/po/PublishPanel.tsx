import { CircleCheck, CloudUpload, ExternalLink } from "lucide-react";
import { usePublishPoSession } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import type { PoSession } from "../../../types/api";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { Input } from "../../ui/input";
import { backlogDraft, defectsForStory, describeCounts, jiraUrl, NEEDS_PROJECT_KEY_REASON, plural } from "./draftOps";

function TicketLink({ base, label, ticketKey }: { base: string | null; label: string; ticketKey: string }) {
  const href = jiraUrl(base, ticketKey);
  return (
    <span className="whitespace-nowrap">
      <span className="text-muted-foreground">{label}</span>{" "}
      {href ? (
        <a className="underline-offset-2 hover:underline" href={href} target="_blank" rel="noreferrer">
          {ticketKey}
        </a>
      ) : (
        ticketKey
      )}
    </span>
  );
}

interface PublishPanelProps {
  projectId: number;
  session: PoSession;
  jiraBaseUrl: string | null;
  onClose: () => void;
  // Unsaved edits in the editor: what is published is what is saved.
  hasUnsavedEdits: boolean;
  // The JIRA project typed when the session did not know it. Held by the page, because
  // a story's own Publish button needs it too.
  typedKey: string;
  onTypedKeyChange: (key: string) => void;
}

// The step that writes to JIRA, for the whole draft — each story has its own button for
// one at a time. Laid out so nothing in it is a surprise: what will be created and where,
// why it cannot be if it cannot, what has been created if it stopped, and — once done —
// every key with a link. Always behind a person's click and the PO agent's own switch
// (po_jira_create_enabled).
export function PublishPanel({ projectId, session, jiraBaseUrl, onClose, hasUnsavedEdits, typedKey, onTypedKeyChange }: PublishPanelProps) {
  const publish = usePublishPoSession(projectId, session.id);
  const { showToast } = useToast();
  const result = session.publish_result;
  const created = result?.created ?? {};
  const createdCount = Object.keys(created).length;
  const state = session.publish;
  const projectKey = state.jira_project_key ?? typedKey.trim().toUpperCase();
  const bug = session.mode === "bug";

  if (session.status === "discarded") return null;

  function press() {
    publish.mutate(
      { jiraProjectKey: state.jira_project_key ? null : projectKey || null },
      {
        onSuccess: () => showToast(bug ? "Publishing — the bug appears in JIRA in a moment" : "Publishing — tickets appear in JIRA as they are made", "success"),
        onError: (err) => showToast(err.message || "Could not publish", "error"),
      },
    );
  }

  if (session.status === "published") {
    const epicKey = result?.epic_key;
    const epicHref = epicKey ? jiraUrl(jiraBaseUrl, epicKey) : null;
    return (
      <Alert variant="success" className="text-foreground block" data-testid="po-published">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-2 font-medium">
            <CircleCheck className="text-success size-4" />
            Published — {plural(createdCount, "ticket", "tickets")} created in {session.jira_project_key}
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-1 text-sm">
            {Object.entries(created).map(([ref, key]) => (
              <TicketLink key={ref} base={jiraBaseUrl} label={ref} ticketKey={key} />
            ))}
          </div>
          {result?.synced ? (
            <p className="text-muted-foreground text-xs">
              {bug && epicKey === created.bug
                ? `${epicKey} is on Elstin's board now, paused — resume it when you want the fix built.`
                : bug
                  ? `${epicKey} is on Elstin's board with the bug under it — resume it when you want the fix built.`
                  : `${epicKey} is on Elstin's board now, paused — resume it when you want the stories run.`}
            </p>
          ) : (
            <p className="text-warning-foreground text-xs">
              The tickets exist in JIRA, but Elstin has not brought them onto its board
              {result?.sync_error ? ` (${result.sync_error})` : ""}. Use "Add ticket" with {epicKey ?? "the epic's key"}.
            </p>
          )}
          <div className="flex gap-2">
            {epicHref ? (
              <Button asChild variant="outline" size="sm">
                <a href={epicHref} target="_blank" rel="noreferrer">
                  <ExternalLink />
                  Open {epicKey} in JIRA
                </a>
              </Button>
            ) : null}
            <Button variant="outline" size="sm" onClick={onClose}>
              Back to the board
            </Button>
          </div>
        </div>
      </Alert>
    );
  }

  if (session.status === "publishing") {
    return (
      <div className="bg-card flex items-center gap-3 rounded-xl border p-3" data-testid="po-publishing">
        <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent" aria-hidden="true" />
        <div className="flex min-w-0 flex-col">
          <span className="text-sm font-medium">Publishing to {session.jira_project_key}…</span>
          <span className="text-muted-foreground text-xs">
            {createdCount === 0 ? "Starting." : `${plural(createdCount, "ticket", "tickets")} created so far.`} The draft is locked until this ends.
          </span>
        </div>
      </div>
    );
  }

  // Not yet published: the preview, the way to do it, and why not when it cannot be.
  const stopped = result?.failed;
  const needsKey = !state.jira_project_key;
  const canPress = state.allowed || (needsKey && !!projectKey && state.reason === NEEDS_PROJECT_KEY_REASON);
  // Some stories (and, in a brainstorm, the epic) were published one at a time already.
  const partial = createdCount > 0;
  const inJira = Object.entries(created).filter(([ref]) => !ref.includes("."));
  // Stories the server found a blocking problem in, as it stands in the saved draft — with
  // edits pending those findings may point at the wrong story, so none are counted.
  const withProblems = hasUnsavedEdits ? 0 : (backlogDraft(session.draft)?.stories ?? []).filter((_, i) => defectsForStory(session.problems, i).length > 0).length;
  return (
    <div className="bg-card flex flex-col gap-2.5 rounded-xl border px-4 py-3.5 shadow-xs" data-testid="po-publish-panel">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <CloudUpload className="text-muted-foreground size-5 shrink-0" aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <h2 className="text-sm font-medium">
            {partial ? "Publish the rest to JIRA" : "Publish to JIRA"}
            {projectKey ? ` project ${projectKey}` : ""}
          </h2>
          <p className="text-muted-foreground text-xs">
            Creates {describeCounts(session.counts)}.
            {withProblems > 0 ? ` ${withProblems === 1 ? "1 story has a problem" : `${withProblems} stories have problems`} to fix first.` : ""}
          </p>
        </div>
        {needsKey ? (
          <Input
            aria-label="JIRA project key"
            className="h-8 w-28"
            placeholder="PROJ"
            value={typedKey}
            onChange={(e) => onTypedKeyChange(e.target.value)}
          />
        ) : null}
        <Button
          size="sm"
          disabled={!canPress || hasUnsavedEdits || session.counts.stories + session.counts.epics + session.counts.bugs === 0}
          loading={publish.isPending}
          onClick={press}
        >
          {partial ? "Publish the rest" : bug ? "Publish" : "Publish all"}
        </Button>
      </div>
      {partial ? (
        <p className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid="po-in-jira">
          <span>Already in JIRA:</span>
          {inJira.map(([ref, key]) => (
            <TicketLink key={ref} base={jiraBaseUrl} label={ref === "epic" ? "epic" : ref} ticketKey={key} />
          ))}
        </p>
      ) : null}
      {partial && result?.synced === false ? (
        <p className="text-warning-foreground text-xs">
          Elstin has not brought what exists onto its board
          {result.sync_error ? ` (${result.sync_error})` : ""}. Use "Add ticket" with {result.epic_key ?? "the epic's key"}.
        </p>
      ) : null}
      {hasUnsavedEdits ? <p className="text-muted-foreground text-xs">Save your edits first: what is published is what is saved.</p> : null}
      {!state.allowed && state.reason && !(needsKey && canPress) ? (
        <p className="text-muted-foreground text-xs" data-testid="po-publish-reason">
          {state.reason}
        </p>
      ) : null}
      {stopped ? (
        <Alert variant="warning" className="text-foreground block">
          <AlertDescription className="flex flex-col gap-1">
            <span>
              A publish stopped after creating {plural(createdCount, "ticket", "tickets")}: {stopped.error}
            </span>
            <span className="text-xs">Press Publish again to continue — what exists is kept and nothing is made twice.</span>
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}
