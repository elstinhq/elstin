import type { EpicPhase, Story, StoryStatus } from "../../types/api";
import { Badge } from "../ui/badge";

type TagVariant = "default" | "secondary" | "destructive" | "success" | "warning" | "info" | "outline";

export const PHASE_APPEARANCE: Record<EpicPhase, TagVariant> = {
  complete: "success",
  failed: "destructive",
  planning: "warning",
  implementing: "info",
};

export const STATUS_APPEARANCE: Record<StoryStatus, TagVariant> = {
  merged: "success",
  done: "success",
  failed: "destructive",
  awaiting_plan_approval: "warning",
  awaiting_pr_open: "warning",
  awaiting_pr_review: "warning",
  dispatched: "info",
  pending: "secondary",
  skipped: "secondary",
};

export const STATUS_LABEL: Record<StoryStatus, string> = {
  merged: "merged",
  done: "done",
  failed: "failed",
  awaiting_plan_approval: "plan needs approval",
  awaiting_pr_open: "ready to open PR",
  awaiting_pr_review: "PR needs review",
  dispatched: "dispatched",
  pending: "pending",
  skipped: "skipped",
};

// A story parks at one of three human gates, and the same click means a
// different thing at each: approving a plan starts development, approving
// the PR-open gate creates the pull request, approving the review merges it.
// A bare "Approve" on all three made a row at the second gate read like one
// at the third, so the button names what it does and says what it won't.
export interface GateAction {
  label: string;
  hint: string;
  // Shown in place of the button once the click has landed and the daemon
  // has not picked it up yet.
  approved: string;
}

export const GATE_ACTION: Partial<Record<StoryStatus, GateAction>> = {
  awaiting_plan_approval: {
    label: "Approve plan",
    hint: "Approve the plan so the dev agent starts writing code.",
    approved: "approved — starting dev",
  },
  awaiting_pr_open: {
    label: "Open PR",
    hint: "Open a pull request on GitHub for this branch. Nothing is merged yet.",
    approved: "approved — opening PR",
  },
  awaiting_pr_review: {
    label: "Approve & merge",
    hint: "Approve this pull request. It is merged into the epic's integration branch once CI passes.",
    approved: "approved — merging",
  },
};

export const ACTIVITY_TYPE_LABEL: Record<string, string> = {
  status: "Status",
  thinking: "Thinking",
  tool_use: "Tool",
  llm_call: "LLM call",
  error: "Error",
};

export const ACTIVITY_PHASE_LABEL: Record<string, string> = {
  planner: "Planner",
  dev: "Dev",
  verifier: "Verifier",
  qa: "QA",
};

interface TagProps {
  label: string;
  appearance?: TagVariant;
  title?: string;
}

export function Tag({ label, appearance = "secondary", title }: TagProps) {
  return (
    <Badge variant={appearance} title={title}>
      {label}
    </Badge>
  );
}

export function StatusTagFor({ status }: { status: StoryStatus }) {
  return <Tag label={STATUS_LABEL[status] || status} appearance={STATUS_APPEARANCE[status]} />;
}

export function PhaseTagFor({ phase }: { phase: EpicPhase }) {
  return <Tag label={phase} appearance={PHASE_APPEARANCE[phase]} />;
}

// The ticket's source changed it after a plan or run was built on the old
// text; the new text is waiting for a person. Shown only while it is.
export function ExternalChangedTag({ story }: { story: Story }) {
  if (!story.external_pending) return null;
  return <Tag label="changed in source" appearance="warning" title="Open the story to apply, re-plan, or dismiss the change" />;
}

// The source reports the ticket done, closed or gone. A story that had not
// started is skipped; one in flight is only flagged, which is what the
// title says, so nobody reads the tag as "forge stopped it".
export function ClosedExternallyTag({ story }: { story: Story }) {
  if (!story.external_closed) return null;
  const inFlight = story.status !== "skipped" && story.status !== "merged" && story.status !== "done";
  return (
    <Tag
      label="closed in source"
      appearance="secondary"
      title={inFlight ? `${story.external_status ?? "Closed"} in the source — this run was already under way and will finish` : (story.external_status ?? "Closed in the source")}
    />
  );
}

// Only QA tickets get a badge. "dev" is the overwhelming majority and the
// default, so tagging every row with it would be noise that makes the one
// row worth noticing harder to spot.
export function KindTag({ story }: { story: Story }) {
  if ((story.kind || "dev") !== "qa") return null;
  return <Tag label="QA" appearance="info" />;
}

// Tests passed; only the coverage-threshold check fell short — distinct
// from a real implementation failure (see forge/tools/vue_tools.py).
export function CoverageGapTag({ story }: { story: Story }) {
  if (!story.coverage_gap) return null;
  return <Tag label="Coverage gap" appearance="warning" />;
}

// _maybe_open_pull_request (forge/daemon/main.py) is best-effort — a push
// or GitHub API failure never blocks the story's own gate progress, so a
// story can sit at "awaiting PR review" with no real PR behind it. This is
// the only visible sign of that short of reading server logs; it's cleared
// the moment a PR-open attempt succeeds, so its presence always means "the
// most recent attempt failed," not stale history.
export function PrOpenErrorTag({ story }: { story: Story }) {
  if (!story.pr_open_error) return null;
  return <Tag label="PR not opened" appearance="destructive" title={story.pr_open_error} />;
}
