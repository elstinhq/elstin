import type { ActivityEvent } from "../../../types/api";
import { plural } from "./draftOps";

// What each of the PO agent's read-only tools was asked about, and the argument that names it
// (elstin/tools/po_tools.py and the repository tools). Distinct values, so reading one file
// five times is one file read, not five.
const SUBJECT: Record<string, { kind: "files" | "pages" | "tickets"; arg: string }> = {
  read_file_tool: { kind: "files", arg: "path" },
  outline_file_tool: { kind: "files", arg: "path" },
  confluence_get_page: { kind: "pages", arg: "page_id" },
  jira_get_issue: { kind: "tickets", arg: "key" },
};

// "Read 14 files, 2 Confluence pages, 3 tickets" — a line for the folded research card.
// Null while nothing that names a file, page or ticket has been read. Event details carry
// the call's arguments as text that may be cut short, so the one value wanted is picked out
// by name rather than by parsing the whole of it.
export function summarizeResearch(events: ActivityEvent[]): string | null {
  const seen = { files: new Set<string>(), pages: new Set<string>(), tickets: new Set<string>() };
  for (const ev of events) {
    const tool = /^Called tool: (\w+)/.exec(ev.title)?.[1];
    const subject = tool ? SUBJECT[tool] : undefined;
    if (!subject || !ev.detail) continue;
    const value = new RegExp(`"${subject.arg}"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"`).exec(ev.detail)?.[1];
    if (value) seen[subject.kind].add(value);
  }
  const parts = [
    seen.files.size ? plural(seen.files.size, "file", "files") : null,
    seen.pages.size ? plural(seen.pages.size, "Confluence page", "Confluence pages") : null,
    seen.tickets.size ? plural(seen.tickets.size, "ticket", "tickets") : null,
  ].filter((p): p is string => p !== null);
  return parts.length ? `Read ${parts.join(", ")}` : null;
}
