import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, Braces, FileText, Trash2 } from "lucide-react";
import { useCancelPoSession, useDeletePoSession, usePoSession } from "../../../hooks/usePoSessions";
import { useProject } from "../../../hooks/useProjects";
import { useProjectConfig } from "../../../hooks/useProjectConfig";
import { api } from "../../../lib/api";
import { useToast } from "../../../lib/toast";
import { ActivityFeed } from "../ActivityFeed";
import { AppShell } from "../../shared/AppShell";
import { PO_MODE_LABEL, PoStatusTag, Tag } from "../../shared/StatusTag";
import { Alert, AlertDescription } from "../../ui/alert";
import { Button } from "../../ui/button";
import { DraftEditor } from "./DraftEditor";
import { PoChat } from "./PoChat";
import { PublishPanel } from "./PublishPanel";
import { QuestionsPanel } from "./QuestionsPanel";

const ENDED = new Set(["discarded"]);

// A backlog draft on its own page (/projects/<id>/po/<session>), in two panes that fill
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
  const title = session?.draft?.epic.summary || (session ? `Backlog draft #${session.id}` : "Backlog draft");

  useEffect(() => {
    document.title = `${title} — Elstin`;
  }, [title]);

  // Leaving the draft behind (discarded, deleted, published and done with) is a
  // page load back to the board — the board's own queries are then fresh.
  function leave() {
    window.location.assign(projectHref);
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

  const back = (
    <a href={projectHref} className="text-muted-foreground hover:text-foreground inline-flex w-fit items-center gap-1 text-sm">
      <ArrowLeft className="size-4" />
      {projectQuery.data?.name ?? "Project"}
    </a>
  );

  // Not this project's draft, or no such draft: say so rather than show another project's work here.
  const missing = query.isError && !session;
  const elsewhere = session != null && session.project_id !== projectId;
  if (missing || elsewhere || !Number.isInteger(sessionId) || sessionId <= 0) {
    return (
      <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
        <div className="flex flex-col gap-3">
          {back}
          <Alert variant="destructive">
            <AlertDescription>
              {elsewhere ? "This draft belongs to another project." : query.error?.message || "There is no such draft."}
            </AlertDescription>
          </Alert>
        </div>
      </AppShell>
    );
  }

  const open = session != null && !ENDED.has(session.status) && !["publishing", "published"].includes(session.status);
  return (
    <AppShell rightLinkHref="/settings" rightLinkLabel="Global settings">
      {/* The window's height less the shell's header and padding: the panes below scroll
          on their own, so the page itself does not (and the conversation never leaves view). */}
      <div className="flex flex-col gap-5 lg:h-[calc(100dvh-7rem)]">
        <header className="flex shrink-0 flex-col gap-2">
          {back}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="min-w-0 text-xl font-semibold">{title}</h1>
            {session ? <PoStatusTag status={session.status} /> : null}
            {session ? <Tag label={PO_MODE_LABEL[session.mode] ?? session.mode} appearance="outline" /> : null}
            {session?.epic_key ? <span className="text-muted-foreground text-sm">{session.epic_key}</span> : null}
            <div className="ml-auto flex flex-wrap items-center gap-1">
              {session?.draft ? (
                <>
                  <Button variant="ghost" size="sm" onClick={() => copy("markdown")}>
                    <FileText />
                    Copy markdown
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => copy("json")}>
                    <Braces />
                    Copy JSON
                  </Button>
                </>
              ) : null}
              {open ? (
                confirming ? (
                  <>
                    <Button variant="destructive" size="sm" loading={cancel.isPending} onClick={discard}>
                      Discard this draft
                    </Button>
                    <Button variant="outline" size="sm" disabled={cancel.isPending} onClick={() => setConfirming(false)}>
                      Keep it
                    </Button>
                  </>
                ) : (
                  <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive" onClick={() => setConfirming(true)}>
                    <Trash2 />
                    Discard
                  </Button>
                )
              ) : null}
              {session && session.status === "published" ? (
                <Button variant="ghost" size="sm" loading={remove.isPending} onClick={deleteSession}>
                  <Trash2 />
                  Delete this record
                </Button>
              ) : null}
            </div>
          </div>
          <p className="text-muted-foreground text-xs">
            {session ? `Draft v${session.draft_version}` : "Loading…"}
            {session && session.cost_usd > 0 ? ` · $${session.cost_usd.toFixed(2)} so far` : ""}
          </p>
        </header>

        {session ? (
          <div className="grid min-h-0 grid-cols-1 gap-6 lg:flex-1 lg:grid-cols-[minmax(0,1fr)_24rem] lg:grid-rows-[minmax(0,1fr)]">
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
              <DraftEditor projectId={projectId} session={session} jiraBaseUrl={jiraBaseUrl} onDirtyChange={setDirty} typedKey={typedKey} />
            </div>
            <aside className="flex min-w-0 flex-col gap-3 lg:min-h-0" aria-label="Agent">
              <ActivityFeed
                streamUrl={api.poActivityStreamUrl(session.id)}
                enabled
                collapsible
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
    </AppShell>
  );
}
