// Mirrors the `dict`/`list[dict]` shapes returned by forge/api/*.py — see
// forge/state/manager_project.py, forge/state/manager_epic.py,
// forge/state/manager_story.py, forge/state/manager_pr.py,
// forge/services/usage.py, forge/services/run_activity.py, and
// forge/config/store.py for the Python side of each of these.

export interface Project {
  id: number;
  name: string;
  description: string | null;
  ticket_source: string;
  repo_count: number;
  epic_count: number;
  created_at: string;
  repos?: ProjectRepo[]; // only present on GET /api/projects/{id}
}

export interface ProjectRepo {
  id: number;
  project_id: number;
  path: string;
  label: string | null;
  clone_url: string | null;
  created_at: string;
}

// One saved JIRA query per project — the background-ingest alternative to
// adding tickets one key at a time. `jql` is compiled server-side from the
// structured fields (forge/services/jira_filter.py's build_jql) and is
// read-only here; it is null when the filter is too empty to compile.
export interface TicketFilter {
  project_id: number;
  enabled: boolean;
  jira_project_key: string | null;
  assignee: string | null;
  statuses: string[];
  labels: string[];
  issue_types: string[];
  extra_jql: string | null;
  jql: string | null;
  last_synced_at: string | null;
  last_matched_count: number | null;
  last_synced_count: number | null;
  last_error: string | null;
}

// The editable subset — what PUT/preview/sync accept.
export interface TicketFilterInput {
  enabled: boolean;
  jira_project_key: string | null;
  assignee: string | null;
  statuses: string[];
  labels: string[];
  issue_types: string[];
  extra_jql: string | null;
}

export interface TicketFilterMatch {
  key: string;
  summary: string;
  issue_type: string;
  status: string;
  action: "sync" | "refresh" | "skip";
  reason: string;
}

export interface TicketFilterPreview {
  jql: string;
  matched: number;
  truncated: boolean;
  matches: TicketFilterMatch[];
  would_sync: string[];
  would_refresh: string[];
}

export interface TicketFilterSyncResult {
  jql: string;
  matched: number;
  truncated: boolean;
  synced: string[];
  refreshed: string[];
  skipped: TicketFilterMatch[];
  errors: { key: string; error: string }[];
}

export interface PullRequest {
  story_key: string;
  number: number | null;
  url: string | null;
  state: string;
}

export type StoryStatus =
  | "pending"
  | "dispatched"
  | "awaiting_plan_approval"
  | "awaiting_pr_open"
  | "awaiting_pr_review"
  | "merged"
  | "done"
  | "failed"
  | "skipped";

export type StoryKind = "dev" | "qa";

// Text the ticket's source changed after forge planned or started the story,
// held for a person to decide on. Only the fields that changed are present.
export interface ExternalPending {
  summary?: string;
  description?: string;
  hash: string;
  reasons: string[];
}

export type ExternalChangeAction = "accept" | "replan" | "dismiss";

export interface Story {
  jira_key: string;
  epic_key: string;
  parent_key: string | null;
  summary: string | null;
  description: string | null;
  wave: number;
  status: StoryStatus;
  kind: StoryKind;
  kind_reason: string | null;
  thread_id: string | null;
  worktree_path: string | null;
  branch: string | null;
  parked_reason: string | null;
  gate_approved: boolean;
  coverage_gap: boolean;
  coverage_gap_detail: string | null;
  pr_open_error: string | null;
  retry_count: number;
  failure_stage: string | null;
  external_source: string | null;
  external_id: string | null;
  external_status: string | null;
  external_closed: boolean;
  external_pending: ExternalPending | null;
  external_changed_at: string | null;
  pull_request: PullRequest | null;
}

export type EpicPhase = "planning" | "implementing" | "complete" | "failed";

export interface EpicLevel {
  wave: number;
  story_keys: string[];
}

export interface Epic {
  jira_key: string;
  project_id: number;
  summary: string | null;
  description: string | null;
  project_repo_id: number | null;
  repo: string | null;
  phase: EpicPhase;
  levels: EpicLevel[];
  integration_branch: string | null;
  plan_approved: boolean;
  manual_gate: boolean;
  external_source: string | null;
  external_id: string | null;
  external_status: string | null;
  external_closed: boolean;
  stories: Story[];
}

export interface ConfigField {
  key: string;
  label: string;
  type: "bool" | "string";
  secret: boolean;
  value: string | null;
  is_set: boolean;
  overridden?: boolean; // only present on project-scoped config
}

export interface ActivityEvent {
  // Server-side row id, ascending. Doubles as the SSE event id, so it is
  // both the React key and how the stream hook drops replayed duplicates
  // after a reconnect.
  id: number;
  type: "status" | "thinking" | "tool_use" | "llm_call" | "error";
  phase: "planner" | "dev" | "verifier" | "qa" | null;
  title: string;
  detail: string | null;
  ts: string;
}

export interface UsageTotals {
  input_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  duration_ms: number;
  api_duration_ms: number | null;
  invocation_count: number;
  has_estimated_cost: boolean;
  has_unknown_cost: boolean;
}

export interface UsagePhase {
  phase: string;
  models: unknown[];
  totals: UsageTotals;
}

export interface StoryUsage {
  story_key: string;
  run_id: string | null;
  phases: UsagePhase[];
  totals: UsageTotals;
}

export interface EpicUsageLevel {
  wave: number;
  stories: { story_key: string; wave: number; totals: UsageTotals }[];
  totals: UsageTotals;
}

export interface EpicUsage {
  epic_key: string;
  levels: EpicUsageLevel[];
  stories: { story_key: string; wave: number; totals: UsageTotals }[];
  totals: UsageTotals;
}
