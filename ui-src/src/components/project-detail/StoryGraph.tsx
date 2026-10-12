import { memo, useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
  type NodeTypes,
} from "@xyflow/react";
import type { Story, StoryStatus } from "../../types/api";
import { StatusTagFor } from "../shared/StatusTag";
import { displayKey } from "../../lib/ticketSource";
import { cn } from "../../lib/utils";

// The epic's stories as a dependency graph: one column per wave (the order
// the epic runs in — elstin/services/epic_sync.py), an arrow from each story
// to the ones that wait for it. Read-only: the graph reflects what the
// daemon decided, it never edits dependencies. Sub-tasks are not shown —
// like the progress bar, only top-level stories are dispatched.
const COL_WIDTH = 270;
const ROW_HEIGHT = 112;
const NODE_WIDTH = 220;

const BORDER: Record<StoryStatus, string> = {
  merged: "border-success",
  done: "border-success",
  failed: "border-destructive",
  awaiting_plan_approval: "border-warning",
  ready_to_merge: "border-info",
  merging: "border-info",
  dispatched: "border-info",
  pending: "border-border",
  skipped: "border-border opacity-60",
};

interface StoryNodeData extends Record<string, unknown> {
  story: Story;
}

const StoryNode = memo(function StoryNode({ data }: NodeProps<Node<StoryNodeData>>) {
  const { story } = data;
  const running = story.status === "dispatched" || story.status === "merging";
  return (
    <div
      className={cn(
        "bg-card text-card-foreground w-[220px] cursor-pointer rounded-lg border-2 px-3 py-2 shadow-sm transition-shadow hover:shadow-md",
        BORDER[story.status] ?? "border-border",
        running && "ring-info/25 ring-4",
      )}
      title={story.summary ?? undefined}
    >
      <Handle type="target" position={Position.Left} className="!bg-muted-foreground !size-2 !border-0" />
      <div className="flex items-center justify-between gap-2">
        <span className="font-mono text-xs font-bold">{displayKey(story)}</span>
        {story.kind === "qa" ? <span className="text-muted-foreground text-[10px] uppercase">QA</span> : null}
      </div>
      <div className="mt-0.5 line-clamp-2 min-h-8 text-xs leading-4">{story.summary || "(no summary)"}</div>
      <div className="mt-1.5">
        <StatusTagFor status={story.status} />
      </div>
      <Handle type="source" position={Position.Right} className="!bg-muted-foreground !size-2 !border-0" />
    </div>
  );
});

const nodeTypes: NodeTypes = { story: StoryNode };

// Lays stories out by wave, then orders each column by the average row of
// the stories it waits for (one left-to-right sweep) so edges mostly run
// straight instead of crossing. Not a full layered-graph layout — it's the
// cheap version, enough for the handful-to-dozens of stories an epic has.
export function layoutStories(stories: Story[]) {
  const keys = new Set(stories.map((s) => s.jira_key));
  const deps = (s: Story) => (s.depends_on ?? []).filter((k) => keys.has(k) && k !== s.jira_key);

  const waves = [...new Set(stories.map((s) => s.wave ?? 0))].sort((a, b) => a - b);
  const row = new Map<string, number>();
  const positions = new Map<string, { x: number; y: number }>();

  waves.forEach((wave, col) => {
    const members = stories
      .filter((s) => (s.wave ?? 0) === wave)
      .map((s) => {
        const rows = deps(s).map((k) => row.get(k)).filter((r): r is number => r !== undefined);
        const bary = rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Number.POSITIVE_INFINITY;
        return { s, bary };
      })
      .sort((a, b) => a.bary - b.bary || a.s.jira_key.localeCompare(b.s.jira_key, undefined, { numeric: true }));
    members.forEach(({ s }, i) => {
      row.set(s.jira_key, i);
      positions.set(s.jira_key, { x: col * COL_WIDTH, y: i * ROW_HEIGHT });
    });
  });

  const nodes: Node<StoryNodeData>[] = stories.map((s) => ({
    id: s.jira_key,
    type: "story",
    position: positions.get(s.jira_key) ?? { x: 0, y: 0 },
    data: { story: s },
    draggable: false,
    connectable: false,
    width: NODE_WIDTH,
  }));

  const byKey = new Map(stories.map((s) => [s.jira_key, s]));
  const edges: Edge[] = stories.flatMap((s) =>
    deps(s).map((from) => {
      const upstream = byKey.get(from)!;
      const landed = upstream.status === "merged" || upstream.status === "done";
      return {
        id: `${from}->${s.jira_key}`,
        source: from,
        target: s.jira_key,
        // Moving dashes on the edge into a story that is running right now.
        animated: s.status === "dispatched",
        markerEnd: { type: MarkerType.ArrowClosed },
        style: { stroke: landed ? "var(--success)" : "var(--muted-foreground)", strokeWidth: 1.5 },
      } satisfies Edge;
    }),
  );
  return { nodes, edges, hasEdges: edges.length > 0 };
}

export function StoryGraph({ stories, onOpenStory }: { stories: Story[]; onOpenStory: (key: string) => void }) {
  // Re-laid-out on every poll (5s) — cheap, and a status change has to
  // reach the node anyway. Keyed on content that affects the picture so an
  // unchanged poll returns the same objects and React Flow does not re-measure.
  const signature = stories
    .map((s) => `${s.jira_key}:${s.status}:${s.wave}:${(s.depends_on ?? []).join(",")}:${s.summary ?? ""}`)
    .join("|");
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const { nodes, edges, hasEdges } = useMemo(() => layoutStories(stories), [signature]);

  const cols = new Set(stories.map((s) => s.wave ?? 0)).size;
  const rows = Math.max(0, ...nodes.map((n) => n.position.y / ROW_HEIGHT)) + 1;
  const height = Math.min(560, Math.max(220, rows * ROW_HEIGHT + 60));

  return (
    <div className="flex flex-col gap-1.5">
      <div className="border-border bg-muted/20 overflow-hidden rounded-lg border" style={{ height }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodeClick={(_, node) => onOpenStory(node.id)}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          fitView
          fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
          minZoom={0.3}
          // The page scrolls; wheel-zooming the canvas would trap the scroll.
          zoomOnScroll={false}
          preventScrolling={false}
          proOptions={{ hideAttribution: false }}
        >
          <Background gap={20} size={1} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <p className="text-muted-foreground text-xs">
        {cols} {cols === 1 ? "wave" : "waves"}, left to right.{" "}
        {hasEdges ? "An arrow means the story on the right waits for the one on the left." : "No dependencies between these stories."}
      </p>
    </div>
  );
}
