import { AlertTriangle, BookOpen, CheckCircle2, FileCode, Ticket, X } from "lucide-react";
import type { PoEvidence } from "../../../types/api";
import { cn } from "../../../lib/utils";
import { confluenceUrl, jiraUrl } from "./draftOps";

const ICON = { code: FileCode, jira: Ticket, confluence: BookOpen } as const;

// One citation: where the agent says a claim comes from. The quote is its title,
// and the tick says whether the server found that quote in what the agent read —
// a claim nobody could confirm is shown as such, not hidden.
function Chip({ evidence, jiraBaseUrl, onRemove }: { evidence: PoEvidence; jiraBaseUrl: string | null; onRemove?: () => void }) {
  const Icon = ICON[evidence.source] ?? FileCode;
  const href =
    evidence.source === "jira" ? jiraUrl(jiraBaseUrl, evidence.ref) : evidence.source === "confluence" ? confluenceUrl(jiraBaseUrl, evidence.ref) : null;
  const unverified = evidence.verified === false;
  const body = (
    <>
      <Icon className="size-3 shrink-0" aria-hidden="true" />
      <span className="max-w-[16rem] truncate">{evidence.ref}</span>
      {evidence.verified === true ? <CheckCircle2 className="text-success size-3 shrink-0" aria-label="quote verified" /> : null}
      {unverified ? <AlertTriangle className="text-warning size-3 shrink-0" aria-label="could not be verified" /> : null}
    </>
  );
  const className = cn(
    "border-border inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs",
    unverified && "border-warning/60",
    href && "hover:bg-accent",
  );
  const title = `${evidence.quote}${unverified ? "\n\n(this quote could not be found in what the agent read)" : ""}`;
  return (
    <span className="inline-flex items-center gap-0.5">
      {href ? (
        <a className={className} href={href} target="_blank" rel="noreferrer" title={title}>
          {body}
        </a>
      ) : (
        <span className={className} title={title}>
          {body}
        </span>
      )}
      {onRemove ? (
        <button type="button" className="text-muted-foreground hover:text-foreground rounded p-0.5" onClick={onRemove} aria-label={`Remove evidence ${evidence.ref}`}>
          <X className="size-3" />
        </button>
      ) : null}
    </span>
  );
}

export function EvidenceChips({
  evidence,
  jiraBaseUrl,
  onRemove,
}: {
  evidence: PoEvidence[];
  jiraBaseUrl: string | null;
  onRemove?: (index: number) => void;
}) {
  if (evidence.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {evidence.map((item, index) => (
        <Chip
          key={`${item.source}:${item.ref}:${index}`}
          evidence={item}
          jiraBaseUrl={jiraBaseUrl}
          onRemove={onRemove ? () => onRemove(index) : undefined}
        />
      ))}
    </div>
  );
}
