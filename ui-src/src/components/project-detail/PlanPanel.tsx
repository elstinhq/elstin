import { useState } from "react";
import { useStoryPlan } from "../../hooks/useStories";
import type { Plan } from "../../types/api";
import { Button } from "../ui/button";
import { MermaidDiagram } from "../shared/MermaidDiagram";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

// The planner's own markdown write-up is the readable form of a plan;
// feature_summary is the only fallback worth showing when a plan predates
// plan_markdown or an older run never produced one.
function planText(plan: Plan): string {
  return (plan.plan_markdown as string | undefined) || (plan.feature_summary as string | undefined) || "";
}

const PREVIEW_LINES = 10;

// A fenced ```mermaid block, which the planner uses for flow charts. The
// fences must start their own line.
const MERMAID_FENCE = /^```mermaid[^\n]*\n([\s\S]*?)^```[ \t]*$/gm;

type Segment = { kind: "text"; text: string } | { kind: "mermaid"; code: string };

function splitSegments(text: string): Segment[] {
  const segments: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(MERMAID_FENCE)) {
    if (match.index > last) segments.push({ kind: "text", text: text.slice(last, match.index) });
    segments.push({ kind: "mermaid", code: (match[1] ?? "").trim() });
    last = match.index + match[0].length;
  }
  if (last < text.length) segments.push({ kind: "text", text: text.slice(last) });
  return segments;
}

function truncateToLines(text: string, maxLines: number): { preview: string; truncated: boolean } {
  let end = 0;
  for (let i = 0; i < maxLines; i++) {
    const next = text.indexOf("\n", end);
    if (next === -1) return { preview: text, truncated: false };
    end = next + 1;
  }
  // Never cut a diagram in half: its unclosed fence would show as raw source.
  for (const match of text.matchAll(MERMAID_FENCE)) {
    if (match.index < end && end < match.index + match[0].length) {
      end = match.index + match[0].length;
      break;
    }
  }
  const preview = text.slice(0, end).trimEnd();
  return { preview, truncated: end < text.trimEnd().length };
}

// Text stays monospace, as before; only the diagrams are drawn.
function PlanText({ text, className }: { text: string; className: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {splitSegments(text).map((segment, i) =>
        segment.kind === "mermaid" ? (
          <MermaidDiagram key={i} code={segment.code} />
        ) : segment.text.trim() ? (
          <pre key={i} className={className}>
            {segment.text.trim()}
          </pre>
        ) : null,
      )}
    </div>
  );
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
      <PlanText
        text={preview + (truncated ? "\n…" : "")}
        className="border-border bg-accent/30 max-h-[220px] min-w-0 overflow-hidden rounded-md border p-2 font-mono text-xs whitespace-pre-wrap"
      />

      <Dialog open={fullOpen} onOpenChange={setFullOpen}>
        <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>Plan — {storyKey}</DialogTitle>
          </DialogHeader>
          <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
            <PlanText text={text} className="min-w-0 font-mono text-xs whitespace-pre-wrap" />
          </div>
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
