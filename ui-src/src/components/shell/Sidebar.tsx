import { useState } from "react";
import { ChevronRight, Plus, SlidersHorizontal } from "lucide-react";
import { useInbox } from "../../hooks/useInbox";
import { Link, matchPath, useLocation } from "../../lib/router";
import { cn } from "../../lib/utils";
import type { InboxProject } from "../../types/api";
import { CreateProjectModal } from "../projects/CreateProjectModal";
import { ThemeToggle } from "../shared/ThemeToggle";
import { Button } from "../ui/button";
import { Skeleton } from "../ui/skeleton";

const STORAGE_KEY = "elstin.sidebar.expanded";

function readExpanded(): Set<number> {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    return new Set(Array.isArray(saved) ? saved.filter((id): id is number => typeof id === "number") : []);
  } catch {
    return new Set();
  }
}

function writeExpanded(ids: Set<number>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...ids]));
  } catch {
    /* remembering is a convenience */
  }
}

export type ProjectSection = "tasks" | "folders" | "lessons" | "backlog" | "settings";

/** Which project and section the URL is on, for highlighting. */
export function useActiveSection(): { projectId: number | null; section: ProjectSection | null } {
  const { pathname } = useLocation();
  const match = matchPath("/projects/:id", pathname) ?? matchPathPrefix(pathname);
  if (!match) return { projectId: null, section: null };
  const projectId = Number(match.id);
  const section = (match.section ?? "tasks") as ProjectSection;
  return { projectId: Number.isFinite(projectId) ? projectId : null, section };
}

function matchPathPrefix(pathname: string): Record<string, string> | null {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] !== "projects" || parts.length < 3) return null;
  // The PO pages' old URLs are the backlog section.
  const section = parts[2] === "po" ? "backlog" : parts[2]!;
  return { id: parts[1]!, section };
}

function CountBadge({ value, tone = "muted" }: { value: number; tone?: "muted" | "attention" }) {
  if (tone === "attention") {
    return (
      <span className="bg-warning text-warning-foreground inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-semibold tabular-nums">
        {value}
      </span>
    );
  }
  return <span className="text-muted-foreground text-xs tabular-nums">{value}</span>;
}

function SectionLink({
  href,
  label,
  active,
  count,
  dot = false,
}: {
  href: string;
  label: string;
  active: boolean;
  count?: number;
  dot?: boolean;
}) {
  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "relative flex items-center gap-2 rounded-md px-3 py-1.5 text-sm transition-colors",
        active ? "bg-accent text-foreground font-medium" : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      {active ? <span className="bg-foreground absolute top-1.5 bottom-1.5 -left-[13px] w-0.5 rounded-full" aria-hidden="true" /> : null}
      <span className="flex-1 truncate">{label}</span>
      {dot ? <span className="bg-warning size-2 rounded-full" aria-hidden="true" /> : null}
      {count !== undefined ? <CountBadge value={count} /> : null}
    </Link>
  );
}

function ProjectNode({
  project,
  expanded,
  onToggle,
  active,
}: {
  project: InboxProject;
  expanded: boolean;
  onToggle: () => void;
  active: ProjectSection | null;
}) {
  const base = `/projects/${project.id}`;
  const { counts } = project;
  return (
    <li>
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${project.name}`}
          className="text-muted-foreground hover:text-foreground rounded p-1"
        >
          <ChevronRight className={cn("size-4 transition-transform", expanded && "rotate-90")} />
        </button>
        <Link
          href={`${base}/tasks`}
          className={cn("min-w-0 flex-1 truncate py-1.5 text-sm", active ? "text-foreground font-semibold" : "text-foreground/90 hover:text-foreground")}
        >
          {project.name}
        </Link>
        {counts.needs_you > 0 ? (
          <span title={`${counts.needs_you} waiting on you`} className="pr-1">
            <CountBadge value={counts.needs_you} tone="attention" />
          </span>
        ) : null}
      </div>
      {expanded ? (
        <div className="border-border mt-0.5 mb-2 ml-3 flex flex-col gap-0.5 border-l pl-3">
          <SectionLink href={`${base}/tasks`} label="Tasks" active={active === "tasks"} count={counts.epics} />
          <SectionLink href={`${base}/folders`} label="Folders" active={active === "folders"} count={counts.repos} />
          <SectionLink
            href={`${base}/lessons`}
            label="Lessons"
            active={active === "lessons"}
            count={counts.lessons_proposed || undefined}
            dot={counts.lessons_proposed > 0}
          />
          <SectionLink href={`${base}/backlog`} label="Backlog drafts" active={active === "backlog"} count={counts.drafts || undefined} />
          <SectionLink href={`${base}/settings`} label="Settings" active={active === "settings"} />
        </div>
      ) : null}
    </li>
  );
}

export function Sidebar() {
  const inbox = useInbox();
  const { pathname } = useLocation();
  const { projectId: activeProject, section } = useActiveSection();
  const [expanded, setExpanded] = useState<Set<number>>(readExpanded);
  const [creating, setCreating] = useState(false);
  const projects = inbox.data?.projects ?? [];

  function toggle(id: number, open: boolean) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (open) next.add(id);
      else next.delete(id);
      writeExpanded(next);
      return next;
    });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex h-16 shrink-0 items-center border-b px-6">
        <Link href="/" className="text-lg font-semibold tracking-tight">
          Elstin
        </Link>
      </div>

      <nav aria-label="Projects" className="min-h-0 flex-1 overflow-y-auto px-3 py-4">
        <div className="flex items-center justify-between px-2 pb-2">
          <Link
            href="/"
            className={cn("text-muted-foreground hover:text-foreground text-sm", pathname === "/" && "text-foreground font-medium")}
          >
            Projects
          </Link>
          <Button variant="ghost" size="icon" className="size-7" aria-label="New project" title="New project" onClick={() => setCreating(true)}>
            <Plus />
          </Button>
        </div>
        {inbox.isPending ? (
          <div className="flex flex-col gap-2 px-2">
            <Skeleton className="h-6" />
            <Skeleton className="h-6 w-3/4" />
          </div>
        ) : projects.length === 0 ? (
          <p className="text-muted-foreground px-2 text-xs">No projects yet.</p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {projects.map((project) => {
              const isActive = project.id === activeProject;
              // The project being viewed is always open; others remember.
              const isExpanded = isActive || expanded.has(project.id);
              return (
                <ProjectNode
                  key={project.id}
                  project={project}
                  expanded={isExpanded}
                  onToggle={() => toggle(project.id, !isExpanded)}
                  active={isActive ? section : null}
                />
              );
            })}
          </ul>
        )}
      </nav>

      <div className="flex shrink-0 items-center gap-2 border-t px-3 py-3">
        <Link
          href="/settings"
          aria-current={pathname === "/settings" ? "page" : undefined}
          className={cn(
            "hover:bg-accent flex flex-1 items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
            pathname === "/settings" ? "bg-accent font-medium" : "text-foreground/90",
          )}
        >
          <SlidersHorizontal className="size-4" />
          Global settings
        </Link>
        <ThemeToggle />
      </div>

      <CreateProjectModal open={creating} onClose={() => setCreating(false)} />
    </div>
  );
}
