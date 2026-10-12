import { useMemo, useState } from "react";
import { Braces, FileText, Trash2 } from "lucide-react";
import { useCancelPoSession, useDeletePoSession, usePoSession } from "../../../hooks/usePoSessions";
import { useProject } from "../../../hooks/useProjects";
import { useProjectConfig } from "../../../hooks/useProjectConfig";
import { api } from "../../../lib/api";
import { useToast } from "../../../lib/toast";
import { ActivityFeed } from "../ActivityFeed";
import { navigate } from "../../../lib/router";
import { Page } from "../../shell/Page";
import { PO_MODE_LABEL, PoStatusTag, Tag } from "../../shared/StatusTag";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { AgentOrb, agentIsBusy } from "./AgentOrb";
import { BugEditor } from "./BugEditor";
import { DraftEditor } from "./DraftEditor";
import { draftTitle } from "./draftOps";
import { PoChat } from "./PoChat";
import { PublishPanel } from "./PublishPanel";
import { QuestionsPanel } from "./QuestionsPanel";
import { summarizeResearch } from "./researchSummary";

const ENDED = new Set(["discarded"]);

// A backlog draft — or a bug draft, with its own editor — on its own page (/projects/<id>/backlog/<session>), in two panes that fill
// the window: the draft a person edits, which scrolls, and a rail beside it that does not —
// the agent's research folded to one line above, and the conversation with the agent docked
// at the foot of the rail, so it is in reach wherever the draft is scrolled to.
export function PoSessionPage({ projectId, sessionId }: { projectId: number; sessionId: number }) {
  const projectQuery = useProject(projectId);
  const configQuery = useProjectConfig(projectId);
  const query = usePoSession(Number.isInteger(sessionId) && sessionId > 0 ? sessionId : null);
  const session = query.data;
  const { showToast } = useToast();
  const [confirming, setConfirming] = useState(false);
  const [dirty, setDirty] = useState(false);
  // The JIRA project, typed when the session does not know it: the page's Publish and each
  // story's own Publish both send it.
  const [typedKey, setTypedKey] = useState("");

  const cancel = useCancelPoSession(projectId, sessionId);
  const remove = useDeletePoSession(projectId);
  const projectHref = `/projects/${projectId}`;
  const jiraBaseUrl = useMemo(() => configQuery.data?.find((f) => f.key === "jira_base_url")?.value ?? null, [configQuery.data]);
  const noun = session?.mode === "bug" ? "Bug draft" : "Backlog draft";
  const title = draftTitle(session?.draft) || (session ? `${noun} #${session.id}` : noun);

  const crumbs = [
    { label: projectQuery.data?.name ?? "Project", href: `${projectHref}/tasks` },
    { label: "Backlog drafts", href: `${projectHref}/backlog` },
    { label: title },
  ];

  // Leaving the draft behind (discarded, deleted, published and done with)
  // goes back to the project's drafts.
  function leave() {
    navigate(`${projectHref}/backlog`);
  }

  async function copy(format: "markdown" | "json") {
    try {
      const text = await api.exportPoSession(sessionId, format);
      await navigator.clipboard.writeText(text);
      showToast(`Copied the draft as ${format === "json" ? "JSON" : "markdown"}`, "success");
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Could not copy the draft", "error");
    }
  }

  function discard() {
    cancel.mutate(undefined, {
      onSuccess: () => {
        showToast("Discarded", "success");
        leave();
      },
      onError: (err) => {
        setConfirming(false);
        showToast(err.message || "Could not discard this session", "error");
      },
    });
  }

  function deleteSession() {
    remove.mutate(sessionId, {
      onSuccess: () => {
        showToast("Deleted", "success");
        leave();
      },
      onError: (err) => showToast(err.message || "Could not delete this session", "error"),
    });
  }

  // Not this project's draft, or no such draft: say so rather than show another project's work here.
  const missing = query.isError && !session;
  const elsewhere = session != null && session.project_id !== projectId;
  if (missing || elsewhere || !Number.isInteger(sessionId) || sessionId <= 0) {
    return (
      <Page crumbs={crumbs}>
        <Alert variant="destructive">
          <AlertDescription>
            {elsewhere ? "This draft belongs to another project." : query.error?.message || "There is no such draft."}
          </AlertDescription>
        </Alert>
      </Page>
    );
  }

  const open = session != null && !ENDED.has(session.status) && !["publishing", "published"].includes(session.status);
  const actions = (
    <>
      {session?.draft ? (
        <>
          <Button variant="ghost" size="xs" onClick={() => copy("markdown")}>
            <FileText />
            Copy markdown
          </Button>
          <Button variant="ghost" size="xs" onClick={() => copy("json")}>
            <Braces />
            Copy JSON
          </Button>
        </>
      ) : null}
      {open ? (
        confirming ? (
          <>
            <Button variant="destructive" size="xs" loading={cancel.isPending} onClick={discard}>
              Discard this draft
            </Button>
            <Button variant="outline" size="xs" disabled={cancel.isPending} onClick={() => setConfirming(false)}>
              Keep it
            </Button>
          </>
        ) : (
          <Button variant="ghost" size="xs" className="text-muted-foreground hover:text-destructive" onClick={() => setConfirming(true)}>
            <Trash2 />
            Discard
          </Button>
        )
      ) : null}
      {session && session.status === "published" ? (
        <Button variant="ghost" size="xs" loading={remove.isPending} onClick={deleteSession}>
          <Trash2 />
          Delete this record
        </Button>
      ) : null}
    </>
  );
  return (
    <Page crumbs={crumbs} title={title} actions={actions} bleed>
      {/* The window's height less the top bar: the panes below scroll on their
          own, so the page itself does not (and the conversation never leaves view). */}
      <div className="flex flex-col gap-5 px-4 py-6 md:px-8 lg:h-[calc(100dvh-4rem)]">
        <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2">
          <h1 className="min-w-0 text-xl font-semibold tracking-tight">{title}</h1>
          {session ? <PoStatusTag status={session.status} /> : null}
          {session ? <Tag label={PO_MODE_LABEL[session.mode] ?? session.mode} appearance="outline" /> : null}
          {session?.epic_key ? <span className="text-muted-foreground text-sm">{session.epic_key}</span> : null}
          <span className="text-muted-foreground text-xs">
            {session ? `Draft v${session.draft_version}` : "Loading…"}
            {session && session.cost_usd > 0 ? ` · $${session.cost_usd.toFixed(2)} so far` : ""}
          </span>
        </div>

        {session ? (
          <div className="grid min-h-0 grid-cols-1 gap-6 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_22.5rem] lg:grid-rows-[minmax(0,1fr)]">
            {/* `relative`: the screen-reader-only text inside is absolutely positioned, and would
                otherwise sit outside this pane and make the page itself scroll. */}
            <div className="relative flex min-w-0 flex-col gap-5 lg:min-h-0 lg:overflow-y-auto lg:pr-3">
              {session.error ? (
                <Alert variant="destructive">
                  <AlertDescription>{session.error}</AlertDescription>
                </Alert>
              ) : null}
              <QuestionsPanel projectId={projectId} session={session} />
              {session.draft ? (
                <PublishPanel
                  projectId={projectId}
                  session={session}
                  jiraBaseUrl={jiraBaseUrl}
                  onClose={leave}
                  hasUnsavedEdits={dirty}
                  typedKey={typedKey}
                  onTypedKeyChange={setTypedKey}
                />
              ) : null}
              {session.mode === "bug" ? (
                <BugEditor projectId={projectId} session={session} jiraBaseUrl={jiraBaseUrl} onDirtyChange={setDirty} />
              ) : (
                <DraftEditor projectId={projectId} session={session} jiraBaseUrl={jiraBaseUrl} onDirtyChange={setDirty} typedKey={typedKey} />
              )}
            </div>
            <aside className="flex min-w-0 flex-col gap-3 lg:min-h-0" aria-label="Agent">
              <ActivityFeed
                streamUrl={api.poActivityStreamUrl(session.id)}
                enabled
                collapsible
                title="Research"
                summarize={summarizeResearch}
                leading={agentIsBusy(session.status) ? <AgentOrb status={session.status} className="text-info" /> : undefined}
                emptyText="Nothing yet — the agent's research shows here as it happens."
                maxHeightClass="max-h-[320px]"
              />
              <PoChat projectId={projectId} session={session} />
            </aside>
          </div>
        ) : (
          <p className="text-muted-foreground text-sm">Loading…</p>
        )}
      </div>
    </Page>
  );
}
