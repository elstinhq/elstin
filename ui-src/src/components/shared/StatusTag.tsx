import { continuedBranches, doneWorkOutcome, needsChoice, onlyInDefault } from "../../lib/priorWork";
import type { EpicPhase, Story, StoryStatus } from "../../types/api";
import { Badge } from "../ui/badge";

type TagVariant = "default" | "secondary" | "destructive" | "success" | "warning" | "info" | "outline";

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
  // forge/services/external_sync.py: set aside while the source has it On
  // Hold (or another ticket_hold_statuses status), back to pending after.
  on_hold_in_source: "on hold in the source — picked up once it is not",
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
  return <Tag label={PHASE_LABEL[phase] || phase} appearance={PHASE_APPEARANCE[phase]} />;
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

// The plan needs changes forge will not make — in a read-only repo, or a file
// the project says agents must not change. The rest of the story runs; these
// are for a person.
export function ReadOnlyChangesTag({ story }: { story: Story }) {
  const changes = story.read_only_changes ?? [];
  if (!changes.length) return null;
  // Short: repo labels run long (@api-contract-definitions) and the row
  // does not wrap. The paths are in the title and the story's details.
  return (
    <Tag
      label="left to a person"
      appearance="warning"
      title={`forge won't make these; someone has to:\n${changes.map((c) => c.path).join("\n")}`}
    />
  );
}

// A pending story whose dependencies have not all merged yet — it starts on
// its own once they have. Says which, so a waiting row is never mistaken for
// a stuck one.
export function unmetDependencies(story: Story, statusByKey: Map<string, string>): string[] {
  if (story.status !== "pending" || !story.depends_on?.length) return [];
  return story.depends_on.filter((key) => {
    const status = statusByKey.get(key);
    return status !== undefined && status !== "merged" && status !== "done" && status !== "skipped";
  });
}

export function WaitingOnTag({ story, statusByKey }: { story: Story; statusByKey: Map<string, string> }) {
  const waiting = unmetDependencies(story, statusByKey);
  if (!waiting.length) return null;
  return <Tag label={`waits for ${waiting.join(", ")}`} appearance="secondary" title="Starts once these have merged into the epic branch" />;
}

// Work that already existed for this ticket before forge made its branch
// (forge/services/prior_work.py): the branch it continues from, or that the
// work is already in the default branch — so a story that "does nothing new"
// is never a surprise. A pending story still waiting for a branch pick shows
// the Choose branch button instead (PriorWorkChoice.tsx).
export function PriorWorkTag({ story }: { story: Story }) {
  if (story.status === "skipped") return <DoneWorkTag story={story} />;
  const continued = continuedBranches(story.prior_work);
  const only = continued.length === 1 ? continued[0] : undefined;
  if (only) {
    const { label, candidate } = only;
    return (
      <Tag
        label={`continues ${candidate.branch}`}
        appearance="info"
        title={`${label}: starts from ${candidate.branch} — ${candidate.ahead} commit(s) of earlier work on this ticket`}
      />
    );
  }
  if (continued.length > 1) {
    return (
      <Tag
        label={`continues ${continued.length} branches`}
        appearance="info"
        title={continued.map(({ label, candidate }) => `${label}: ${candidate.branch}`).join("\n")}
      />
    );
  }
  if (story.status === "pending" && needsChoice(story.prior_work).length) {
    return <Tag label="pick a branch" appearance="warning" title="Several branches carry diverged work for this ticket" />;
  }
  if (onlyInDefault(story.prior_work)) {
    return (
      <Tag
        label="earlier work in main"
        appearance="secondary"
        title="Work for this ticket is already in the default branch; the planner checks what, if anything, is still missing"
      />
    );
  }
  return null;
}

// A ticket closed as done in its source is not run — but its branch may hold
// work the default branch does not have yet, which forge merges into the
// epic branch (forge/daemon/main.py's _bring_done_work_into_epic).
function DoneWorkTag({ story }: { story: Story }) {
  const results = Object.entries(story.prior_work?.epic_merge ?? {});
  const lines = results.map(([label, r]) => `${label}: ${r.branch ?? ""} ${r.detail ? `— ${r.detail}` : ""}`.trim()).join("\n");
  switch (doneWorkOutcome(story.prior_work)) {
    case "merged":
      return <Tag label="work merged into epic" appearance="info" title={`Done in the source; its branch was not in the default branch yet, so it was merged into the epic branch.\n${lines}`} />;
    case "conflict":
      return <Tag label="work conflicts" appearance="warning" title={`Done in the source, but its branch conflicts with the epic branch — not merged. A person has to resolve it.\n${lines}`} />;
    case "choose":
      return <Tag label="pick its branch" appearance="warning" title="Done in the source; several branches carry diverged work — pick the one to merge into the epic branch" />;
    case "not_done":
      return <Tag label="work not taken" appearance="secondary" title={`Closed as ${story.external_status ?? "dropped"}, so its branch is not merged into the epic branch.`} />;
    default:
      return null;
  }
}

// Landing it on the epic branch sent it back to dev (a conflict with a story
// that merged meanwhile, or the combined code failing its checks).
export function MergeAttemptsTag({ story }: { story: Story }) {
  if (!story.merge_attempts || story.status === "merged") return null;
  return (
    <Tag
      label={`re-landing ${story.merge_attempts}×`}
      appearance="warning"
      title="Bringing it up to date with the epic branch needed another dev pass"
    />
  );
}

// A "failed" story that never got past planning and one where dev actually
// wrote code but the build or the reviewer rejected it read the same in the
// plain status tag — the second is real progress, not a dead end, and is
// worth a click into the run before retrying blind. gate_runs/dev_passes
// are the cheapest signal that dev actually ran (see forge/api/
// epic_routes.py's _story_with_pr): the deterministic gate only runs after
// dev writes something.
export function CodeWrittenTag({ story }: { story: Story }) {
  const outcome = story.run_outcome;
  if (story.status !== "failed" || story.failure_stage !== "run" || !outcome) return null;
  if (outcome.dev_passes <= 0 && outcome.gate_runs <= 0) return null;
  const detail =
    outcome.verifier_rejections > 0
      ? "The reviewer rejected the implementation — open the run to see why."
      : outcome.gate_failures > 0
        ? "The build/test gate failed after dev wrote code — often an environment issue (toolchain version, missing dependency), not the code itself."
        : "Dev wrote code before this run failed — open it to see what happened.";
  return <Tag label="Code written, verification failed" appearance="warning" title={detail} />;
}
