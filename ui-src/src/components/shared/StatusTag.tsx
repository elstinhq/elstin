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
  awaiting_plan_approval: "awaiting plan approval",
  awaiting_pr_open: "awaiting PR open approval",
  awaiting_pr_review: "awaiting PR review",
  dispatched: "dispatched",
  pending: "pending",
  skipped: "skipped",
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
