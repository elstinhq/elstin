import { useState } from "react";
import { ArrowRight } from "lucide-react";
import { Link } from "../../lib/router";
import { cn } from "../../lib/utils";
import type { InboxItem, InboxKind } from "../../types/api";
import { InboxItemRow } from "./InboxItemRow";

interface Group {
  id: string;
  title: string;
  kinds: InboxKind[];
  // Lessons are not holding up any ticket, so their dot is quieter.
  urgent: boolean;
  // How many items a card shows before "N more" — a lesson is read in full,
  // so its card shows one.
  shown?: number;
}

export const INBOX_GROUPS: Group[] = [
  { id: "plans", title: "Plans to review", kinds: ["plan_approval", "questions"], urgent: true },
  { id: "prs", title: "Pull requests to open", kinds: ["pr_approval"], urgent: true },
  { id: "changed", title: "Changed in the source", kinds: ["external_change"], urgent: true },
  { id: "failed", title: "Failed runs", kinds: ["failed"], urgent: true },
  { id: "drafts", title: "Backlog drafts", kinds: ["po_draft"], urgent: true },
  { id: "lessons", title: "Lessons to review", kinds: ["lesson"], urgent: false, shown: 1 },
];


function GroupCard({ group, items, moreHref }: { group: Group; items: InboxItem[]; moreHref?: string }) {
  const [all, setAll] = useState(false);
  const shown = all ? items : items.slice(0, group.shown ?? 2);
  const more = items.length - shown.length;
  return (
    <section className="bg-card flex min-w-0 flex-col rounded-xl border p-5 shadow-xs" aria-label={group.title}>
      <header className="flex items-center gap-2.5 border-b pb-3">
        <span className={cn("size-2 rounded-full", group.urgent ? "bg-warning" : "bg-muted-foreground/40")} aria-hidden="true" />
        <h3 className="flex-1 text-sm font-semibold">{group.title}</h3>
        <span className="text-muted-foreground text-sm tabular-nums">{items.length}</span>
      </header>
      <div className="flex flex-col gap-4 pt-3">
        {shown.map((item, i) => (
          <InboxItemRow key={`${item.kind}-${item.story_key ?? item.epic_key ?? item.lesson_id ?? item.session_id}-${i}`} item={item} />
        ))}
      </div>
      {more > 0 ? (
        moreHref ? (
          <Link href={moreHref} className="text-muted-foreground hover:text-foreground mt-3 ml-auto inline-flex items-center gap-1 text-sm">
            {more} more <ArrowRight className="size-3.5" />
          </Link>
        ) : (
          <button type="button" onClick={() => setAll(true)} className="text-muted-foreground hover:text-foreground mt-3 ml-auto inline-flex items-center gap-1 text-sm">
            {more} more <ArrowRight className="size-3.5" />
          </button>
        )
      ) : null}
    </section>
  );
}

// "Needs you": one card per kind of gate that has something in it. A gate
// left empty is not shown, and nothing pending at all is one quiet line.
export function NeedsYouCards({ items, projectId }: { items: InboxItem[]; projectId?: number }) {
  const groups = INBOX_GROUPS.map((group) => ({ group, items: items.filter((i) => group.kinds.includes(i.kind)) })).filter((g) => g.items.length);
  if (!groups.length) {
    return <p className="text-muted-foreground bg-card rounded-xl border border-dashed px-5 py-4 text-sm">Nothing is waiting on you.</p>;
  }
  const moreHref = (id: string) => {
    if (projectId === undefined) return undefined;
    if (id === "lessons") return `/projects/${projectId}/lessons`;
    if (id === "drafts") return `/projects/${projectId}/backlog`;
    return undefined;
  };
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
      {groups.map(({ group, items: groupItems }) => (
        <GroupCard key={group.id} group={group} items={groupItems} moreHref={moreHref(group.id)} />
      ))}
    </div>
  );
}
