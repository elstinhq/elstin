import type { PoAnyDraft, PoBugDraft, PoCounts, PoDefect, PoDraft, PoStory } from "../../../types/api";

// Pure edits of a draft, so the editor's components only say what changed.
// Every one returns a new draft; none mutates.

export function isBugDraft(draft: PoAnyDraft | null | undefined): draft is PoBugDraft {
  return draft != null && draft.mode === "bug";
}

// The session's draft when it is a backlog (epic and stories), else null.
export function backlogDraft(draft: PoAnyDraft | null | undefined): PoDraft | null {
  return draft != null && !isBugDraft(draft) ? draft : null;
}

// What a draft is called: its bug's summary, else its epic's.
export function draftTitle(draft: PoAnyDraft | null | undefined): string {
  if (!draft) return "";
  return isBugDraft(draft) ? draft.bug.summary : draft.epic.summary;
}

export function bugWithoutBlanks(draft: PoBugDraft): PoBugDraft {
  const clean = (items: string[]) => items.map((i) => i.trim()).filter(Boolean);
  const bug = draft.bug;
  return {
    ...draft,
    bug: {
      ...bug,
      steps_to_reproduce: clean(bug.steps_to_reproduce),
      to_confirm: clean(bug.to_confirm),
      acceptance_criteria: clean(bug.acceptance_criteria),
      labels: clean(bug.labels),
    },
    assumptions: clean(draft.assumptions),
    out_of_scope: clean(draft.out_of_scope),
  };
}

export function nextRef(stories: PoStory[]): string {
  const used = new Set(stories.map((s) => s.ref));
  for (let n = stories.length + 1; ; n += 1) {
    if (!used.has(`s${n}`)) return `s${n}`;
  }
}

export function newStory(ref: string): PoStory {
  return {
    ref,
    summary: "",
    description: "",
    acceptance_criteria: [""],
    issue_type: "Story",
    kind: "dev",
    labels: [],
    depends_on: [],
    subtasks: [],
    evidence: [],
    rationale: "",
    size: "M",
  };
}

export function updateStory(draft: PoDraft, ref: string, patch: Partial<PoStory>): PoDraft {
  return { ...draft, stories: draft.stories.map((s) => (s.ref === ref ? { ...s, ...patch } : s)) };
}

// A removed story takes its refs out of every dependency list, or the draft
// would depend on something that is gone.
export function removeStory(draft: PoDraft, ref: string): PoDraft {
  return {
    ...draft,
    stories: draft.stories
      .filter((s) => s.ref !== ref)
      .map((s) => (s.depends_on.includes(ref) ? { ...s, depends_on: s.depends_on.filter((d) => d !== ref) } : s)),
  };
}

// Blank lines are what an "add" button leaves behind; they are not content and
// the server would call an empty criterion a stub.
export function withoutBlanks(draft: PoDraft): PoDraft {
  const clean = (items: string[]) => items.map((i) => i.trim()).filter(Boolean);
  return {
    ...draft,
    epic: { ...draft.epic, acceptance_criteria: clean(draft.epic.acceptance_criteria) },
    stories: draft.stories.map((s) => ({
      ...s,
      acceptance_criteria: clean(s.acceptance_criteria),
      labels: clean(s.labels),
      subtasks: s.subtasks.filter((t) => t.summary.trim()),
    })),
    gaps: clean(draft.gaps),
    assumptions: clean(draft.assumptions),
    out_of_scope: clean(draft.out_of_scope),
  };
}

// Findings about one story, from the server's `where` paths into the saved
// draft ("stories[2].acceptance_criteria").
export function defectsForStory(defects: PoDefect[], index: number): PoDefect[] {
  const prefix = `stories[${index}]`;
  return defects.filter((d) => d.where === prefix || d.where?.startsWith(`${prefix}.`) || d.where?.startsWith(`${prefix}[`));
}

export function defectsOutsideStories(defects: PoDefect[]): PoDefect[] {
  return defects.filter((d) => !d.where?.startsWith("stories["));
}

export function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

// "1 epic, 6 stories, 4 sub-tasks and 3 links" — only the parts there are.
export function describeCounts(counts: PoCounts): string {
  const parts = [
    counts.bugs ? plural(counts.bugs, "bug", "bugs") : null,
    counts.epics ? plural(counts.epics, "epic", "epics") : null,
    counts.stories ? plural(counts.stories, "story", "stories") : null,
    counts.subtasks ? plural(counts.subtasks, "sub-task", "sub-tasks") : null,
    counts.links ? plural(counts.links, "link", "links") : null,
  ].filter((p): p is string => p !== null);
  if (parts.length === 0) return "nothing";
  return parts.length === 1 ? parts[0]! : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

// The one reason the server gives that a person clears by typing the JIRA project key
// beside the Publish button (elstin/services/po_sessions.py's NEEDS_PROJECT_KEY_REASON).
export const NEEDS_PROJECT_KEY_REASON = "Say which JIRA project the tickets go in.";

export function jiraUrl(base: string | null, key: string): string | null {
  return base ? `${base.replace(/\/+$/, "")}/browse/${encodeURIComponent(key)}` : null;
}

// Confluence Cloud serves a page by id at /wiki/pages/viewpage.action.
export function confluenceUrl(base: string | null, ref: string): string | null {
  const id = /(?:\/pages\/|pageId=)(\d+)|^(\d+)$/.exec(ref);
  const pageId = id ? (id[1] ?? id[2]) : null;
  return base && pageId ? `${base.replace(/\/+$/, "")}/wiki/pages/viewpage.action?pageId=${pageId}` : null;
}
