import { useState } from "react";
import { Bug, ChevronRight, Filter, SearchX, Sparkles } from "lucide-react";
import { usePoSessions } from "../../hooks/usePoSessions";
import { useTicketFilter } from "../../hooks/useTicketFilter";
import { Link } from "../../lib/router";
import { relativeTime } from "../../lib/time";
import type { PoSessionSummary, Project } from "../../types/api";
import { FoldersPanel } from "../project-detail/FoldersPanel";
import { LessonsPanel } from "../project-detail/LessonsPanel";
import { ProjectSettingsPanel } from "../project-detail/ProjectSettingsPanel";
import { TicketFilterDialog } from "../project-detail/TicketFilterDialog";
import { Page } from "../shell/Page";
import { EmptyState } from "../shared/EmptyState";
import { PO_MODE_LABEL, PoStatusTag, Tag } from "../shared/StatusTag";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";

const crumb = (project: Project) => ({ label: project.name, href: `/projects/${project.id}/tasks` });

export function FoldersView({ project }: { project: Project }) {
  return (
    <Page crumbs={[crumb(project), { label: "Folders" }]} title={`${project.name} · Folders`}>
      <FoldersPanel projectId={project.id} />
    </Page>
  );
}

export function LessonsView({ project }: { project: Project }) {
  return (
    <Page crumbs={[crumb(project), { label: "Lessons" }]} title={`${project.name} · Lessons`}>
      <div className="max-w-4xl">
        <LessonsPanel projectId={project.id} />
      </div>
    </Page>
  );
}

const dash = <span className="text-muted-foreground">–</span>;

// A draft stays listed after it is published — that is the record of which
// tickets it made — until it is deleted from its own page; a discarded one is
// hidden by the server.
//
// The whole row opens the draft: the title is the link, stretched over the row
// (`after:inset-0`), so it stays a real link — keyboard, middle-click, copy
// address — which a clickable <tr> would not be.
//
// Below the table's container width (`@4xl`) only the draft and its status
// stay as columns; the rest folds into a line under the title.
function DraftRow({ projectId, session }: { projectId: number; session: PoSessionSummary }) {
  const mode = PO_MODE_LABEL[session.mode] ?? session.mode;
  // A bug draft is one ticket: it has no stories to count.
  const stories = session.mode === "bug" ? null : `${session.stories} ${session.stories === 1 ? "story" : "stories"}`;
  const updated = relativeTime(session.updated_at);
  const updatedAt = session.updated_at ? new Date(session.updated_at).toLocaleString() : undefined;
  const cost = session.cost_usd > 0 ? `$${session.cost_usd.toFixed(2)}` : null;
  return (
    <TableRow className="hover:bg-accent/40 focus-within:bg-accent/40 relative">
      <TableCell className="max-w-0 min-w-28 @4xl:w-[28%]">
        <Link
          href={`/projects/${projectId}/backlog/${session.id}`}
          className="line-clamp-2 text-sm font-medium after:absolute after:inset-0 after:content-['']"
        >
          {session.title || session.brief}
        </Link>
        {session.error ? <span className="text-destructive mt-0.5 line-clamp-1 block text-xs">{session.error}</span> : null}
        <span className="text-muted-foreground mt-0.5 block text-xs @4xl:hidden">
          {[mode, stories, session.epic_key, cost, updated].filter(Boolean).join(" · ")}
        </span>
      </TableCell>
      <TableCell className="hidden @4xl:table-cell">
        <Tag label={mode} appearance="outline" />
      </TableCell>
      <TableCell className="w-px @4xl:w-auto">
        <PoStatusTag status={session.status} />
      </TableCell>
      <TableCell className="hidden @4xl:table-cell">
        {session.epic_key ? <span className="text-muted-foreground font-mono text-xs whitespace-nowrap">{session.epic_key}</span> : dash}
      </TableCell>
      <TableCell className="hidden text-sm tabular-nums @4xl:table-cell">{session.mode === "bug" ? dash : session.stories}</TableCell>
      <TableCell className="hidden text-sm tabular-nums @4xl:table-cell">{cost ?? dash}</TableCell>
      <TableCell className="text-muted-foreground hidden text-sm whitespace-nowrap @4xl:table-cell" title={updatedAt}>
        {updated}
      </TableCell>
      <TableCell className="w-10 pl-0 text-right">
        <ChevronRight className="text-muted-foreground ml-auto size-4" aria-hidden="true" />
      </TableCell>
    </TableRow>
  );
}

function DraftsTable({ projectId, sessions }: { projectId: number; sessions: PoSessionSummary[] }) {
  return (
    <div className="@container">
      <div className="bg-card overflow-hidden rounded-xl border shadow-xs">
        <Table>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead>Draft</TableHead>
              <TableHead className="hidden @4xl:table-cell">Mode</TableHead>
              <TableHead className="w-px @4xl:w-auto">Status</TableHead>
              <TableHead className="hidden @4xl:table-cell">Epic</TableHead>
              <TableHead className="hidden @4xl:table-cell">Stories</TableHead>
              <TableHead className="hidden @4xl:table-cell">Cost</TableHead>
              <TableHead className="hidden @4xl:table-cell">Updated</TableHead>
              <TableHead className="w-10">
                <span className="sr-only">Open</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {sessions.map((session) => (
              <DraftRow key={session.id} projectId={projectId} session={session} />
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}

export function BacklogView({ project }: { project: Project }) {
  const query = usePoSessions(project.id);
  const sessions = query.data ?? [];
  return (
    <Page
      crumbs={[crumb(project), { label: "Backlog drafts" }]}
      title={`${project.name} · Backlog drafts`}
      actions={
        <>
          <Button variant="outline" asChild>
            <Link href={`/projects/${project.id}/backlog/new?kind=bug`}>
              <Bug />
              Report a bug
            </Link>
          </Button>
          <Button asChild>
            <Link href={`/projects/${project.id}/backlog/new`}>
              <Sparkles />
              Plan backlog
            </Link>
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground max-w-3xl text-sm">
          Backlogs and bugs the PO agent drafted with you, newest first. A draft stays here after it is published — that is the record
          of which tickets it made — until you delete it from its own page.
        </p>
        {query.isPending ? (
          <Skeleton className="h-32 rounded-xl" />
        ) : sessions.length === 0 ? (
          <EmptyState
            icon={Sparkles}
            heading="No drafts yet"
            body="Start one with Plan backlog — from a brief, or from an epic to split or check for gaps — or Report a bug to have its cause traced and written up as one ticket."
          />
        ) : (
          <DraftsTable projectId={project.id} sessions={sessions} />
        )}
      </div>
    </Page>
  );
}

export function ProjectSettingsView({ project }: { project: Project }) {
  const filter = useTicketFilter(project.id).data;
  const [filterOpen, setFilterOpen] = useState(false);
  const ticketSource = project.ticket_source ?? "jira";
  return (
    <Page crumbs={[crumb(project), { label: "Settings" }]} title={`${project.name} · Settings`}>
      <div className="mx-auto flex max-w-3xl flex-col gap-6">
        <div className="bg-card flex flex-wrap items-center justify-between gap-3 rounded-xl border p-4 shadow-xs">
          <div className="min-w-0">
            <div className="text-sm font-medium">Ticket filter</div>
            <p className="text-muted-foreground text-xs">
              {filter
                ? filter.enabled
                  ? `Adding tickets that match ${filter.jira_project_key || "the saved query"} on their own.`
                  : "A query is saved, but the background sync is off."
                : "No filter — tickets are added by hand with Add ticket."}
            </p>
          </div>
          <Button variant="outline" onClick={() => setFilterOpen(true)}>
            <Filter />
            {filter ? "Edit filter" : "Set up a filter"}
          </Button>
        </div>
        <ProjectSettingsPanel projectId={project.id} ticketSource={ticketSource} />
      </div>
      <TicketFilterDialog projectId={project.id} ticketSource={ticketSource} open={filterOpen} onClose={() => setFilterOpen(false)} />
    </Page>
  );
}

export function NotFoundView({ what = "page" }: { what?: string }) {
  return (
    <Page crumbs={[{ label: "Not found" }]}>
      <EmptyState icon={SearchX} heading={`No such ${what}`} body="It may have been deleted, or the link is wrong." />
      <div className="mt-4 flex justify-center">
        <Button variant="outline" asChild>
          <Link href="/">Back to projects</Link>
        </Button>
      </div>
    </Page>
  );
}
