import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, CircleCheck, CloudUpload, ExternalLink, Plus, Trash2, X } from "lucide-react";
import type { PoCounts, PoDefect, PoStory } from "../../../types/api";
import { cn } from "../../../lib/utils";
import { Tag } from "../../shared/StatusTag";
import { Badge } from "../../ui/badge";
import { Button } from "../../ui/button";
import { Card } from "../../ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../../ui/collapsible";
import { Input } from "../../ui/input";
import { Label } from "../../ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../ui/select";
import { Textarea } from "../../ui/textarea";
import { Tooltip, TooltipContent, TooltipTrigger } from "../../ui/tooltip";
import { describeCounts, jiraUrl, plural } from "./draftOps";
import { EvidenceChips } from "./EvidenceChips";
import { StringList } from "./StringList";

// This story's own Publish button, as the editor works it out: the server's answer
// (po_sessions.story_publish_states), adjusted for what only the page knows — edits not
// yet saved, a project key typed but not yet stored, a publish already under way.
export interface StoryPublishControl {
  allowed: boolean;
  // Why it cannot be pressed, shown on hover; null when it can.
  reason: string | null;
  // What pressing it creates.
  counts: PoCounts;
  // This story's publish is the one in flight.
  busy: boolean;
  onPress: () => void;
}

interface StoryCardProps {
  story: PoStory;
  index: number;
  // Everything this story may depend on besides itself: the draft's other
  // stories, and the epic's existing ones (gap mode).
  others: { key: string; label: string }[];
  defects: PoDefect[];
  readOnly: boolean;
  jiraBaseUrl: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // The JIRA key this story was made as, once it was published. It is then a ticket, and
  // no longer editable here.
  publishedKey: string | null;
  // Absent when nothing can be published from this session (a discarded one).
  publish?: StoryPublishControl;
  onChange: (patch: Partial<PoStory>) => void;
  onRemove: () => void;
}

function Pick<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: T;
  options: readonly T[];
  onChange: (value: T) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-muted-foreground text-xs">{label}</span>
      <Select value={value} onValueChange={(next) => onChange(next as T)} disabled={disabled}>
        <SelectTrigger size="sm" aria-label={label} className="min-w-20">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function PublishedKey({ ticketKey, base }: { ticketKey: string; base: string | null }) {
  const href = jiraUrl(base, ticketKey);
  const body = (
    <>
      <CircleCheck aria-hidden="true" />
      {ticketKey}
      {href ? <ExternalLink aria-hidden="true" /> : null}
    </>
  );
  return href ? (
    <Badge variant="success" asChild>
      <a href={href} target="_blank" rel="noreferrer" title={`Open ${ticketKey} in JIRA`} data-testid="po-story-key">
        {body}
      </a>
    </Badge>
  ) : (
    <Badge variant="success" data-testid="po-story-key">
      {body}
    </Badge>
  );
}

// One proposed story, folded to a line — its summary, what it is, what is wrong with it
// and its own Publish — and opening to everything a person may want to change, with what
// the server found wrong. The ticket it becomes is exactly what is shown here — description
// with the criteria under "Acceptance Criteria" — so there is nothing hidden to be
// surprised by at publish. Once published it is a ticket: shown, not editable.
export function StoryCard({
  story,
  index,
  others,
  defects,
  readOnly: sessionReadOnly,
  jiraBaseUrl,
  open,
  onOpenChange,
  publishedKey,
  publish,
  onChange,
  onRemove,
}: StoryCardProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const readOnly = sessionReadOnly || publishedKey !== null;
  // A ticket already: there is nothing left to fix in it here.
  const shown = publishedKey ? [] : defects;
  const blocking = shown.filter((d) => d.blocking);
  const advice = shown.filter((d) => !d.blocking);
  const detailCount = story.depends_on.length + story.evidence.length + story.subtasks.length;

  function toggleDependency(key: string) {
    onChange({
      depends_on: story.depends_on.includes(key) ? story.depends_on.filter((d) => d !== key) : [...story.depends_on, key],
    });
  }

  // A dependency on something that is no longer in the draft is shown too, so it
  // can be removed instead of lingering unseen.
  const known = new Set(others.map((o) => o.key));
  const dangling = story.depends_on.filter((d) => !known.has(d));

  return (
    <Card className="gap-0 overflow-hidden py-0" data-testid={`po-story-${story.ref}`}>
      <Collapsible open={open} onOpenChange={onOpenChange}>
        <div className="flex items-center gap-2 py-2.5 pr-3 pl-1.5">
          <CollapsibleTrigger
            className="hover:bg-accent/60 flex min-w-0 flex-1 items-center gap-2 rounded-md px-1.5 py-1 text-left transition-colors"
            aria-label={`${open ? "Collapse" : "Expand"} story ${story.ref}`}
          >
            <ChevronRight className={cn("text-muted-foreground size-4 shrink-0 transition-transform", open && "rotate-90")} aria-hidden="true" />
            <Tag label={story.ref} appearance="outline" />
            <span className="flex min-w-0 flex-1 flex-col">
              <span className="truncate text-sm font-medium">
                {story.summary || <span className="text-muted-foreground font-normal italic">Untitled story</span>}
              </span>
              <span className="text-muted-foreground truncate text-xs">
                {story.issue_type} · {story.kind} · {story.size}
                {story.depends_on.length > 0 ? ` · after ${story.depends_on.join(", ")}` : ""}
                {story.subtasks.length > 0 ? ` · ${plural(story.subtasks.length, "sub-task", "sub-tasks")}` : ""}
              </span>
            </span>
          </CollapsibleTrigger>

          {blocking.length > 0 ? (
            <Tag label={blocking.length === 1 ? "1 problem" : `${blocking.length} problems`} appearance="destructive" />
          ) : null}
          {advice.length > 0 && blocking.length === 0 ? <Tag label={advice.length === 1 ? "1 note" : `${advice.length} notes`} appearance="warning" /> : null}

          {publishedKey ? (
            <PublishedKey ticketKey={publishedKey} base={jiraBaseUrl} />
          ) : publish ? (
            <Tooltip>
              <TooltipTrigger asChild>
                {/* A disabled button gets no pointer events, so the span carries the hover. */}
                <span className="inline-flex shrink-0" tabIndex={publish.allowed ? -1 : 0}>
                  <Button
                    variant="outline"
                    size="sm"
                    aria-label={`Publish ${story.ref} to JIRA`}
                    disabled={!publish.allowed}
                    loading={publish.busy}
                    onClick={publish.onPress}
                  >
                    <CloudUpload />
                    Publish
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent side="left" className="max-w-xs">
                {publish.allowed ? `Creates ${describeCounts(publish.counts)} in JIRA` : (publish.reason ?? "Cannot be published now")}
              </TooltipContent>
            </Tooltip>
          ) : null}
        </div>

        <CollapsibleContent>
          <div className="flex flex-col gap-4 border-t px-4 py-4">
            {publishedKey ? (
              <p className="text-muted-foreground bg-muted rounded-md px-3 py-2 text-xs">
                Published as {publishedKey}. It is a ticket now: change it in JIRA — edits here no longer reach it.
              </p>
            ) : null}

            {blocking.map((d, i) => (
              <p key={`b${i}`} className="text-destructive flex items-start gap-1.5 text-xs">
                <AlertTriangle className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                {d.reason}
              </p>
            ))}
            {advice.map((d, i) => (
              <p key={`a${i}`} className="text-warning-foreground bg-warning/15 flex items-start gap-1.5 rounded px-2 py-1 text-xs">
                <AlertTriangle className="mt-0.5 size-3 shrink-0" aria-hidden="true" />
                {d.reason}
              </p>
            ))}

            <div className="flex flex-wrap items-end gap-3">
              <div className="flex min-w-48 flex-1 flex-col gap-1">
                <Label htmlFor={`summary-${story.ref}`} className="text-muted-foreground text-xs font-normal">
                  Summary
                </Label>
                <Input
                  id={`summary-${story.ref}`}
                  aria-label={`Summary of ${story.ref}`}
                  className="font-medium"
                  placeholder="Summary"
                  value={story.summary}
                  disabled={readOnly}
                  onChange={(e) => onChange({ summary: e.target.value })}
                />
              </div>
              <Pick label="Issue type" value={story.issue_type} options={["Story", "Task"] as const} disabled={readOnly} onChange={(issue_type) => onChange({ issue_type })} />
              <Pick label="Kind" value={story.kind} options={["dev", "qa"] as const} disabled={readOnly} onChange={(kind) => onChange({ kind })} />
              <Pick label="Size" value={story.size} options={["S", "M", "L"] as const} disabled={readOnly} onChange={(size) => onChange({ size })} />
            </div>
            {story.kind === "qa" ? <Tag label="runs after every dev story" appearance="info" /> : null}

            <div className="flex flex-col gap-1">
              <Label htmlFor={`description-${story.ref}`} className="text-muted-foreground text-xs font-normal">
                Description
              </Label>
              <Textarea
                id={`description-${story.ref}`}
                aria-label={`Description of ${story.ref}`}
                placeholder="Why this story exists, what is in it and what is not"
                value={story.description}
                disabled={readOnly}
                onChange={(e) => onChange({ description: e.target.value })}
              />
            </div>

            <StringList
              label="Acceptance criteria"
              items={story.acceptance_criteria}
              readOnly={readOnly}
              addLabel="Add criterion"
              onChange={(acceptance_criteria) => onChange({ acceptance_criteria })}
            />

            <Collapsible open={detailsOpen} onOpenChange={setDetailsOpen}>
              <CollapsibleTrigger className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs">
                {detailsOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                Dependencies, sub-tasks, evidence{detailCount > 0 ? ` (${detailCount})` : ""}
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-3 flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium">Depends on</span>
                  {others.length === 0 && dangling.length === 0 ? (
                    <p className="text-muted-foreground text-xs">Nothing else in the draft to depend on.</p>
                  ) : null}
                  {others.map((other) => (
                    <label key={other.key} className="flex items-start gap-2 text-sm">
                      <input
                        type="checkbox"
                        className="mt-1"
                        checked={story.depends_on.includes(other.key)}
                        disabled={readOnly}
                        onChange={() => toggleDependency(other.key)}
                      />
                      <span>
                        <span className="font-medium">{other.key}</span> <span className="text-muted-foreground">{other.label}</span>
                      </span>
                    </label>
                  ))}
                  {dangling.map((key) => (
                    <label key={key} className="text-destructive flex items-start gap-2 text-sm">
                      <input type="checkbox" className="mt-1" checked disabled={readOnly} onChange={() => toggleDependency(key)} />
                      <span>{key} (not in this draft)</span>
                    </label>
                  ))}
                </div>

                <div className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium">Sub-tasks</span>
                  {story.subtasks.map((sub, subIndex) => (
                    <div className="flex items-start gap-1.5" key={subIndex}>
                      <div className="flex min-w-0 flex-1 flex-col gap-1">
                        <Input
                          aria-label={`Sub-task ${subIndex + 1} summary`}
                          placeholder="Sub-task summary"
                          value={sub.summary}
                          disabled={readOnly}
                          onChange={(e) =>
                            onChange({ subtasks: story.subtasks.map((t, i) => (i === subIndex ? { ...t, summary: e.target.value } : t)) })
                          }
                        />
                        <Textarea
                          aria-label={`Sub-task ${subIndex + 1} description`}
                          className="min-h-9 py-1.5"
                          placeholder="Detail (optional)"
                          value={sub.description}
                          disabled={readOnly}
                          onChange={(e) =>
                            onChange({ subtasks: story.subtasks.map((t, i) => (i === subIndex ? { ...t, description: e.target.value } : t)) })
                          }
                        />
                      </div>
                      {!readOnly ? (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="size-8 shrink-0"
                          aria-label={`Remove sub-task ${subIndex + 1}`}
                          onClick={() => onChange({ subtasks: story.subtasks.filter((_, i) => i !== subIndex) })}
                        >
                          <X />
                        </Button>
                      ) : null}
                    </div>
                  ))}
                  {!readOnly ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      className="w-fit"
                      onClick={() => onChange({ subtasks: [...story.subtasks, { summary: "", description: "" }] })}
                    >
                      <Plus />
                      Add sub-task
                    </Button>
                  ) : null}
                </div>

                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`labels-${story.ref}`}>Labels</Label>
                  <Input
                    id={`labels-${story.ref}`}
                    placeholder="comma-separated"
                    value={story.labels.join(", ")}
                    disabled={readOnly}
                    onChange={(e) => onChange({ labels: e.target.value.split(",").map((l) => l.trimStart()) })}
                  />
                  <span className="text-muted-foreground text-xs">
                    A QA story gets the <code>qa</code> label when it is published; add <code>dev</code> or <code>qa</code> here if Elstin's reading of the summary disagrees with the kind.
                  </span>
                </div>

                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`why-${story.ref}`}>Why this story</Label>
                  <Textarea
                    id={`why-${story.ref}`}
                    className="min-h-9 py-1.5"
                    value={story.rationale}
                    disabled={readOnly}
                    onChange={(e) => onChange({ rationale: e.target.value })}
                  />
                </div>

                <div className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium">Evidence</span>
                  {story.evidence.length === 0 ? <p className="text-muted-foreground text-xs">None cited.</p> : null}
                  <EvidenceChips
                    evidence={story.evidence}
                    jiraBaseUrl={jiraBaseUrl}
                    onRemove={readOnly ? undefined : (i) => onChange({ evidence: story.evidence.filter((_, n) => n !== i) })}
                  />
                </div>
              </CollapsibleContent>
            </Collapsible>

            {!readOnly ? (
              <div className="flex justify-end border-t pt-3">
                <Button variant="ghost" size="sm" className="text-muted-foreground hover:text-destructive" aria-label={`Remove story ${story.ref}`} onClick={onRemove}>
                  <Trash2 />
                  Remove story
                </Button>
              </div>
            ) : null}
          </div>
        </CollapsibleContent>
      </Collapsible>
      <span className="sr-only">Story {index + 1}</span>
    </Card>
  );
}
