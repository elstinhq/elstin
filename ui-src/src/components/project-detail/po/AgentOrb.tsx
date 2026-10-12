import { Orb, type OrbState } from "@yogesharc/thinking-orbs";
import type { PoStatus } from "../../../types/api";

// The PO agent at work, as an orb (github.com/yogesharc/thinking-orbs): waiting for the daemon
// to pick the session up, or thinking through the brief and the draft. A session that is not
// moving — waiting on a person, ready, done — has none, and neither has publishing: that is
// the JIRA publisher at work, not the agent, and has its own indicator.
const ORB: Partial<Record<PoStatus, OrbState>> = {
  queued: "waiting",
  running: "reasoning",
};

export const agentIsBusy = (status: PoStatus) => status in ORB;

// Drawn in the text color, so a `text-*` class tints it. It carries no label of its own: it
// always sits beside words that say what is happening, and announcing both would say it twice.
// It holds still under `prefers-reduced-motion`.
export function AgentOrb({ status, size = 20, className }: { status: PoStatus; size?: number; className?: string }) {
  const state = ORB[status];
  return state ? <Orb state={state} size={size} className={className} /> : null;
}
