/** The board's status vocabulary — labels, tag tones, and the five groups
 * every status falls into. Pure data, no components: the legend, the progress
 * bars, the status pills and "Needs you" all read the same groups, so a colour
 * means the same thing everywhere (components/shared/StatusTag.tsx renders
 * these as tags).
 */
import type { EpicPhase, PoAssessment, PoMode, PoStatus, Story, StoryStatus } from "../types/api";

export type TagVariant = "default" | "secondary" | "destructive" | "success" | "warning" | "info" | "outline";

export const PHASE_APPEARANCE: Record<EpicPhase, TagVariant> = {
  merged: "success",
  complete: "success",
  pr_open: "info",
  failed: "destructive",
  planning: "warning",
  implementing: "info",
};

export const PHASE_LABEL: Record<EpicPhase, string> = {
  merged: "merged",
  complete: "complete",
  pr_open: "PR open",
  failed: "failed",
  planning: "planning",
  implementing: "implementing",
};

export const STATUS_APPEARANCE: Record<StoryStatus, TagVariant> = {
  merged: "success",
  done: "success",
  failed: "destructive",
  awaiting_plan_approval: "warning",
  ready_to_merge: "info",
  merging: "info",
  dispatched: "info",
  pending: "secondary",
  skipped: "secondary",
};

export const STATUS_LABEL: Record<StoryStatus, string> = {
  merged: "merged",
  done: "done",
  failed: "failed",
  awaiting_plan_approval: "plan needs approval",
  ready_to_merge: "ready to merge",
  merging: "merging",
  dispatched: "dispatched",
  pending: "pending",
  skipped: "skipped",
};

// The one human gate a story can stop at: plan approval (and the planner's
// clarification questions, answered in the detail dialog). A finished story
// merges into the epic branch by itself; the epic's pull request is the
// review point. The button names what it does.
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
};

// Reasons a story stops that the row can say in words. Anything else is shown
// as the raw code, and a *_gate reason is not shown at all.
export const PARKED_REASON_LABEL: Record<string, string> = {
  already_satisfied: "already satisfied — no changes needed",
  cancelled: "cancelled",
  // elstin/services/external_sync.py: set aside while the source has it On
  // Hold (or another ticket_hold_statuses status), back to pending after.
  on_hold_in_source: "on hold in the source — picked up once it is not",
  // elstin/services/provider_limit.py: the provider refused calls on a rate or
  // quota limit. With a retry time elstin retries the story itself then.
  usage_limit: "provider limit",
  // elstin/services/provider_limit.py: the model endpoint could not be reached
  // (a proxy or VPN blip, usually). Elstin retries the story itself shortly.
  provider_unreachable: "model endpoint unreachable",
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
  system: "System",
  po: "PO",
};

// PO sessions (the backlog the PO agent drafts with a person): their status,
// the mode elstin chose for them, and the agent's verdict on an existing story.
export const PO_STATUS_APPEARANCE: Record<PoStatus, TagVariant> = {
  queued: "secondary",
  running: "info",
  awaiting_input: "warning",
  ready: "success",
  publishing: "info",
  published: "success",
  failed: "destructive",
  discarded: "secondary",
};

export const PO_STATUS_LABEL: Record<PoStatus, string> = {
  queued: "waiting for the agent",
  running: "agent working",
  awaiting_input: "needs your answers",
  ready: "draft ready",
  publishing: "publishing",
  published: "published",
  failed: "failed",
  discarded: "discarded",
};

export const PO_MODE_LABEL: Record<PoMode, string> = {
  brainstorm: "new epic",
  split: "split an epic",
  gap: "gap analysis",
  bug: "bug",
};

export const PO_ASSESSMENT_APPEARANCE: Record<PoAssessment, TagVariant> = {
  done: "success",
  partial: "warning",
  not_started: "secondary",
  obsolete: "outline",
  unclear: "warning",
};

export const PO_ASSESSMENT_LABEL: Record<PoAssessment, string> = {
  done: "done",
  partial: "partly done",
  not_started: "not started",
  obsolete: "obsolete",
  unclear: "unclear",
};


// What a person scanning the board needs to tell apart: done, moving, waiting
// on them, not started, broken. Ordered as the legend shows them.
export type StatusGroup = "merged" | "running" | "needs_you" | "pending" | "failed";

export const STATUS_GROUPS: StatusGroup[] = ["merged", "running", "needs_you", "pending", "failed"];

export const STATUS_GROUP_LABEL: Record<StatusGroup, string> = {
  merged: "merged",
  running: "running",
  needs_you: "needs you",
  pending: "pending",
  failed: "failed",
};

// Background classes for a group's swatch — the progress segments and the legend.
export const STATUS_GROUP_SWATCH: Record<StatusGroup, string> = {
  merged: "bg-success",
  running: "bg-info",
  needs_you: "bg-warning",
  pending: "bg-muted-foreground/25",
  failed: "bg-destructive",
};

/** A story's group. A gate the person already cleared (an approved plan the
 * daemon has not picked up yet) is running, not waiting on them; a held change
 * from the source is theirs whatever the status. */
export function storyGroup(story: Pick<Story, "status" | "gate_approved" | "external_pending">): StatusGroup {
  if (story.external_pending) return "needs_you";
  switch (story.status) {
    case "merged":
    case "done":
      return "merged";
    case "failed":
      return "failed";
    case "awaiting_plan_approval":
      return story.gate_approved ? "running" : "needs_you";
    case "dispatched":
    case "ready_to_merge":
    case "merging":
      return "running";
    default:
      return "pending";
  }
}
