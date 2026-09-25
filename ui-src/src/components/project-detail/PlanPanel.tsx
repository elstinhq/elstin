import { useState } from "react";
import { useStoryPlan } from "../../hooks/useStories";
import type { Plan } from "../../types/api";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

// The planner's own markdown write-up is the readable form of a plan;
// feature_summary is the only fallback worth showing when a plan predates
// plan_markdown or an older run never produced one.
function planText(plan: Plan): string {
  return (plan.plan_markdown as string | undefined) || (plan.feature_summary as string | undefined) || "";
}

const PREVIEW_LINES = 10;

function truncateToLines(text: string, maxLines: number): { preview: string; truncated: boolean } {
  const lines = text.split("\n");
  if (lines.length <= maxLines) return { preview: text, truncated: false };
  return { preview: lines.slice(0, maxLines).join("\n"), truncated: true };
}

export function StoryPlanPanel({ storyKey, open }: { storyKey: string; open: boolean }) {
  const planQuery = useStoryPlan(storyKey, open);
  const [fullOpen, setFullOpen] = useState(false);
  const plan = planQuery.data?.plan;
  const text = plan ? planText(plan) : "";
  if (!text) return null;

  const { preview, truncated } = truncateToLines(text, PREVIEW_LINES);

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-muted-foreground text-xs">Plan</span>
        {truncated ? (
          <Button variant="outline" size="sm" onClick={() => setFullOpen(true)}>
            View full plan
          </Button>
        ) : null}
      </div>
      <pre className="border-border bg-accent/30 max-h-[220px] min-w-0 overflow-hidden rounded-md border p-2 font-mono text-xs whitespace-pre-wrap">
        {preview}
        {truncated ? "\n…" : ""}
      </pre>

      <Dialog open={fullOpen} onOpenChange={setFullOpen}>
        <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Plan — {storyKey}</DialogTitle>
          </DialogHeader>
          <pre className="min-w-0 flex-1 overflow-y-auto font-mono text-xs whitespace-pre-wrap">{text}</pre>
          <DialogFooter>
            <Button variant="outline" onClick={() => setFullOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
