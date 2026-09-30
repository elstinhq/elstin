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

// The guide a run against a repo would read (forge/services/project_guide.py):
// the repo's own AGENTS.md/CLAUDE.md, else one forge generated, else none.
export interface RepoGuide {
  source: "repo" | "generated" | "none";
  filename: string | null;
  content: string | null;
  generated_at: string | null;
}

// "repo": a git repo; "reference": a plain folder read for context (its
// CLAUDE.md, docs), never written — always read_only. A read-only repo is
// read by every run but no plan may change it.
export type ProjectRepoKind = "repo" | "reference";

export interface ProjectRepo {
  id: number;
  project_id: number;
  path: string;
  label: string | null;
  clone_url: string | null;
  kind: ProjectRepoKind;
  read_only: boolean;
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

// Work counters for a story's run (forge/services/usage.py's
// StoryRunOutcome) — null until the story's first dispatch. Accumulate
// across every retry under the same run_id, so on a retried story these
// are the run's lifetime totals, not just the latest attempt.
export interface RunOutcome {
  run_id: string;
  planner_turns: number;
  explorer_count: number;
  dev_passes: number;
  dev_turns: number;
  dev_soft_stops: number;
  gate_runs: number;
  gate_failures: number;
  verifier_runs: number;
  verifier_rejections: number;
  dev_terminal_reason: string | null;
  outcome: string | null;
}

export type StoryStatus =
  | "pending"
  | "dispatched"
  | "awaiting_plan_approval"
  // The run passed; its work waits for the merge queue to land it on the
  // epic branch, locally (forge/daemon/main.py).
  | "ready_to_merge"
  | "merging"
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

// A design decision the planner found genuinely ambiguous — see
// forge/agents/roles/planner.py's CLARIFICATION QUESTIONS rules. Answered
// via POST /api/stories/{key}/answer, one option id per question.
export interface ClarificationOption {
  id: string;
  label: string;
  description: string | null;
}

export interface ClarificationQuestion {
  id: string;
  question: string;
  context: string | null;
  options: ClarificationOption[];
}

// A human's resolved answer, as recorded server-side — not what the form
// submits (that's {question_id, option_id} against ClarificationQuestion).
export interface ClarificationAnswer {
  question: string;
  option_label: string;
  option_description: string | null;
}

// The planner's structured output (forge/agents/roles/planner.py), read
// back from the story's LangGraph checkpoint — see GET
// /api/stories/{key}/plan. Deliberately loose: this is whatever the LLM
// returned as JSON, and the UI only ever reads plan_markdown/feature_summary
// off it rather than depending on the full shape.
export interface Plan {
  feature_summary?: string;
  plan_markdown?: string;
  reasoning?: string;
  [key: string]: unknown;
}

export interface StoryPlan {
  story_key: string;
  // Null before the story's first run has reached the planner (no
  // thread_id yet, or a checkpoint whose plan is still the {} the graph
  // seeds it with) — and always null for a "qa" story, which never plans.
  plan: Plan | null;
}

// A branch that already carried work for a ticket, found before forge made
// its own branch (forge/services/prior_work.py).
export interface PriorWorkCandidate {
  branch: string;
  ref: string;
  tip: string;
  ahead: number;
  behind: number;
  // Its changes are already in the default branch (squash merges included).
  in_default: boolean;
  commits: string[];
  files: string[];
  files_total: number;
  last_author: string | null;
  last_commit_at: string | null;
  unpushed?: boolean;
  diverged_from_origin?: boolean;
  // Other tickets the same branch is named after.
  shared_with?: string[];
  pull_request?: { number: number; url: string; state: "open" | "merged" | "closed" };
}

// "continue": the ticket's branch starts from `chosen`; "choose": several
// diverged, a person picks; "in_default": only already-merged work.
export interface PriorWorkFinding {
  verdict: "continue" | "choose" | "in_default" | "none";
  default_branch: string;
  base: string;
  chosen: string | null;
  candidates: PriorWorkCandidate[];
  default_commits: string[];
}

// One lookup per ticket, keyed by repo label. `picks` is a person's answer
// to a "choose" ("" = start fresh). Null until looked up; `stale` after
// "Look again", until the next poll looks again (keeping picks that apply).
export interface PriorWork {
  looked_at?: string;
  applicable?: boolean;
  keys?: string[];
  repos?: Record<string, PriorWorkFinding>;
  picks?: Record<string, string>;
  errors?: Record<string, string>;
  stale?: boolean;
  // A ticket closed as done: what became of its branch, per repo — merged
  // into the epic branch, already there, conflicting, waiting for a pick,
  // or not taken (closed as dropped).
  epic_merge?: Record<string, EpicMergeResult>;
}

export interface EpicMergeResult {
  result: "merged" | "in_epic" | "conflict" | "choose" | "not_done";
  detail: string | null;
  branch?: string;
  tip?: string;
  at?: string;
}

// A change the ticket needs that forge may not make, so a person has to: in a
// read-only repo or reference folder, or a file the project says agents must
// not change (forge/agents/roles/plan_gate.py's normalize_plan).
export interface ReadOnlyChange {
  path: string;
  purpose: string | null;
}

export interface Story {
  jira_key: string;
  epic_key: string;
  parent_key: string | null;
  summary: string | null;
  description: string | null;
  wave: number;
  // The stories this one waits for; null only for a row synced before
  // dependencies were stored.
  depends_on: string[] | null;
  prior_work: PriorWork | null;
  status: StoryStatus;
  kind: StoryKind;
  kind_reason: string | null;
  thread_id: string | null;
  worktree_path: string | null;
  branch: string | null;
  parked_reason: string | null;
  gate_approved: boolean;
  clarification_questions: ClarificationQuestion[];
  clarification_answers: ClarificationAnswer[];
  coverage_gap: boolean;
  coverage_gap_detail: string | null;
  // From the latest plan; empty when it needs none.
  read_only_changes: ReadOnlyChange[];
  pr_open_error: string | null;
  retry_count: number;
  failure_stage: string | null;
  // Times landing it on the epic branch sent it back to dev.
  merge_attempts: number;
  external_source: string | null;
  external_id: string | null;
  external_status: string | null;
  external_closed: boolean;
  external_pending: ExternalPending | null;
  external_changed_at: string | null;
  pull_request: PullRequest | null;
  run_outcome: RunOutcome | null;
}

// "pr_open": every story merged and the epic's one pull request is open;
// "merged": a person merged it; "complete": done without an open pull request
// (GitHub not configured, nothing to propose, or the PR was closed).
export type EpicPhase = "planning" | "implementing" | "complete" | "pr_open" | "merged" | "failed";

// The epic's pull request, per repo — the only one forge opens.
export interface EpicPullRequest {
  repo_label: string;
  repo_path: string;
  branch: string;
  number: number | null;
  url: string | null;
  state: "open" | "merged" | "closed";
  ci_status: "pending" | "success" | "failure" | null;
  error: string | null;
}

export interface EpicLevel {
  wave: number;
  story_keys: string[];
}

// What removing a ticket did, and what it deliberately did not touch
// (forge/daemon/ticket_removal.py). Removal is local only.
// One folder's subfolders on the machine forge runs on — the folder picker
// browses through forge (POST /api/projects/browse-folder), since a browser
// cannot give a page a real filesystem path.
export interface FolderListing {
  path: string;
  parent: string | null;
  home: string;
  is_repo: boolean;
  entries: { name: string; path: string; is_repo: boolean }[];
  truncated: boolean;
}

// What attaching a folder would mean (POST /api/projects/scan-folder): a git
// repo as is, or a folder holding repos to pick from — forge works inside
// one repository at a time, so each picked one becomes a folder of its own.
export interface ScannedRepo {
  path: string;
  name: string;
  // Why it cannot be attached (no commits yet, say); null when it can.
  problem: string | null;
  attached: boolean;
}

// What the folder itself would give as a reference folder: the top-level
// entries that would be read, and those left out (data files, too large).
export interface ReferencePreview {
  readable: string[];
  skipped: { name: string; reason: string }[];
  bytes: number;
}

export interface ScanFolderResult {
  path: string;
  is_repo: boolean;
  attached: boolean;
  repos: ScannedRepo[];
  reference?: ReferencePreview;
}

// What deleting a project did (forge/daemon/project_removal.py). Local only:
// attached folders stay on disk and nothing on GitHub or JIRA is touched.
export interface DeleteProjectReport {
  project_id: number;
  name: string;
  removed: {
    tickets: string[];
    clones: string[];
    settings_overrides: number;
    ticket_filters: number;
    folders: number;
  };
  attached_folders_kept: string[];
  left_on_remote: {
    pull_requests: { story_key: string; number: number; url: string | null; state: string }[];
    branches_that_may_be_pushed: string[];
  };
  warnings: string[];
}

export interface RemoveTicketReport {
  epic: string;
  removed: {
    stories: number;
    pull_request_records: number;
    activity_events: number;
    usage_rows: number;
    run_outcomes: number;
    lessons_detached: number;
    lessons_dropped: number;
    checkpoint_threads: number;
    worktrees: string[];
    branches: string[];
    directories: string[];
  };
  left_on_remote: {
    pull_requests: { story_key: string; number: number; url: string | null; state: string }[];
    branches_that_may_be_pushed: string[];
  };
  ticket_filter_may_readd: boolean;
  warnings: string[];
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
  prior_work: PriorWork | null;
  pull_requests: EpicPullRequest[];
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
  phase: "planner" | "dev" | "verifier" | "qa" | "system" | null;
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

// A lesson forge proposed from its own runs (forge/services/lessons.py,
// forge/state/manager_lesson.py). Only "accepted" ones reach an agent.
export type LessonStatus = "proposed" | "accepted" | "rejected";

export interface ProjectLesson {
  id: number;
  project_id: number;
  project_repo_id: number | null;
  text: string;
  kind: string;
  scope_globs: string[];
  status: LessonStatus;
  occurrences: number;
  evidence_story_keys: string[];
  evidence_quote: string | null;
  created_at: string | null;
  updated_at: string | null;
}
