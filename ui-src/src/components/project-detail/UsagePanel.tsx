import { RotateCw } from "lucide-react";
import { Tag, ACTIVITY_PHASE_LABEL } from "../shared/StatusTag";
import { useEpicUsage } from "../../hooks/useEpicUsage";
import { useStoryUsage } from "../../hooks/useStories";
import type { UsageTotals } from "../../types/api";
import { Button } from "../ui/button";

function formatUsageTokens(n: number): string {
  if (!n) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function formatUsageCost(totals: UsageTotals): string {
  if (totals.cost_usd == null) return totals.has_unknown_cost ? "cost unavailable" : "—";
  // "≈" flags a cost forge estimated from a static pricing table (see
  // forge/llm/pricing.py) rather than one the provider itself reported —
  // no wired provider adapter reports cost directly today, so this is
  // the common case, not a fallback.
  const prefix = totals.has_estimated_cost ? "≈" : "";
  const value = totals.cost_usd;
  return `${prefix}$${value.toFixed(value < 1 ? 4 : 2)}`;
}

function formatUsageDuration(ms: number): string {
  if (!ms) return "—";
  const totalSeconds = Math.round(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return m > 0 ? `${m}m${String(s).padStart(2, "0")}s` : `${s}s`;
}

function UsageSummaryLine({ totals }: { totals: UsageTotals }) {
  if (!totals.invocation_count) {
    return <span className="text-muted-foreground text-xs tabular-nums">no usage data</span>;
  }
  const tokens = (totals.input_tokens || 0) + (totals.output_tokens || 0);
  const parts = [`${formatUsageTokens(tokens)} tokens`];
  if (totals.cache_read_tokens) parts.push(`${formatUsageTokens(totals.cache_read_tokens)} cached`);
  parts.push(formatUsageCost(totals));
  parts.push(formatUsageDuration(totals.duration_ms));
  return <span className="text-muted-foreground text-xs tabular-nums">{parts.join(" · ")}</span>;
}

function UsageSectionHeader({ title, onRefresh, refreshing }: { title: string; onRefresh: () => void; refreshing: boolean }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-sm font-medium">{title}</span>
      <Button variant="ghost" size="icon" className="size-6" aria-label="Refresh usage" loading={refreshing} onClick={onRefresh}>
        <RotateCw />
      </Button>
    </div>
  );
}

export function StoryUsagePanel({ storyKey }: { storyKey: string }) {
  const usageQuery = useStoryUsage(storyKey);
  const usage = usageQuery.data;

  return (
    <div className="flex flex-col gap-2">
      <UsageSectionHeader title="Usage & cost" onRefresh={() => usageQuery.refetch()} refreshing={usageQuery.isFetching} />
      {!usage || usage.phases.length === 0 ? (
        <p className="text-muted-foreground text-xs">No usage recorded yet for this ticket's current run.</p>
      ) : (
        <div className="flex flex-col gap-2">
          <UsageSummaryLine totals={usage.totals} />
          <div className="flex flex-col gap-2">
            {usage.phases.map((phase) => (
              <div className="border-border flex flex-wrap items-center gap-3 border-b py-2 last:border-b-0" key={phase.phase}>
                <span className="w-[70px] shrink-0">
                  <Tag label={ACTIVITY_PHASE_LABEL[phase.phase] || phase.phase} />
                </span>
                <UsageSummaryLine totals={phase.totals} />
                <span className="text-muted-foreground text-xs">
                  {phase.totals.invocation_count === 1 ? "1 run" : `${phase.totals.invocation_count} runs`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function EpicUsagePanel({ epicKey }: { epicKey: string }) {
  const usageQuery = useEpicUsage(epicKey);
  const usage = usageQuery.data;

  return (
    <div className="flex flex-col gap-2">
      <UsageSectionHeader title="Usage & cost" onRefresh={() => usageQuery.refetch()} refreshing={usageQuery.isFetching} />
      {!usage || !usage.totals.invocation_count ? (
        <p className="text-muted-foreground text-xs">No usage recorded yet for this task's stories.</p>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">Total</span>
            <UsageSummaryLine totals={usage.totals} />
          </div>
          <div className="flex max-h-[320px] flex-col gap-2 overflow-y-auto">
            {usage.levels.map((level) => (
              <div className="border-border rounded-lg border p-2.5" key={level.wave}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">Wave {level.wave}</span>
                  <UsageSummaryLine totals={level.totals} />
                </div>
                {level.stories.map((story) => (
                  <div className="flex flex-wrap items-center gap-3 py-1" key={story.story_key}>
                    <span className="w-[78px] shrink-0 font-mono text-sm">{story.story_key}</span>
                    <UsageSummaryLine totals={story.totals} />
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
