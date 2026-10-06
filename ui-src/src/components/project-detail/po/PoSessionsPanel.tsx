import { usePoSessions } from "../../../hooks/usePoSessions";
import type { PoSessionSummary } from "../../../types/api";
import { PO_MODE_LABEL, PoStatusTag, Tag } from "../../shared/StatusTag";

// The project's open backlog drafts, newest first. A draft stays here after it
// is published — that is the record of which tickets it made — until it is
// deleted from its own page; a discarded one is hidden by the server.
function SessionRow({ projectId, session }: { projectId: number; session: PoSessionSummary }) {
  return (
    <a
      href={`/projects/${projectId}/po/${session.id}`}
      className="border-border hover:bg-accent flex w-full flex-col gap-1 rounded-md border p-2.5 text-left transition-colors"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <PoStatusTag status={session.status} />
        <Tag label={PO_MODE_LABEL[session.mode] ?? session.mode} appearance="outline" />
        {session.epic_key ? <span className="text-muted-foreground text-xs">{session.epic_key}</span> : null}
      </div>
      <span className="text-sm font-medium">{session.title || session.brief}</span>
      <span className="text-muted-foreground text-xs">
        {session.stories} proposed {session.stories === 1 ? "story" : "stories"}
        {session.cost_usd > 0 ? ` · $${session.cost_usd.toFixed(2)}` : ""}
      </span>
      {session.error ? <span className="text-destructive line-clamp-2 text-xs">{session.error}</span> : null}
    </a>
  );
}

export function PoSessionsPanel({ projectId }: { projectId: number }) {
  const query = usePoSessions(projectId);
  const sessions = query.data ?? [];
  // Nothing drafted yet: the panel would only be an empty heading, and the
  // button that starts a draft is already in the column above.
  if (sessions.length === 0) return null;

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold">Backlog drafts</h2>
      <div className="flex flex-col gap-2">
        {sessions.map((session) => (
          <SessionRow key={session.id} projectId={projectId} session={session} />
        ))}
      </div>
    </section>
  );
}
