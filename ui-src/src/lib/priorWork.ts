import type { PriorWork, PriorWorkCandidate } from "../types/api";

// Mirrors forge/services/prior_work.py's needs_choice / start_candidate —
// the two questions the UI asks of a stored lookup.

// Not looked up yet, or "Look again" was pressed and the next poll has not run.
export function lookupDue(prior: PriorWork | null | undefined): boolean {
  return !prior || Boolean(prior.stale);
}

// Repo labels where several branches diverged and nobody has picked one.
export function needsChoice(prior: PriorWork | null | undefined): string[] {
  if (!prior) return [];
  const picks = prior.picks ?? {};
  return Object.entries(prior.repos ?? {})
    .filter(([label, finding]) => finding.verdict === "choose" && !(label in picks))
    .map(([label]) => label)
    .sort();
}

// The branch the ticket's work starts from in `label`, if any.
export function startCandidate(prior: PriorWork | null | undefined, label: string): PriorWorkCandidate | null {
  const finding = prior?.repos?.[label];
  if (!finding) return null;
  const picks = prior?.picks ?? {};
  const wanted = label in picks ? picks[label] : finding.chosen;
  if (!wanted) return null;
  return finding.candidates.find((c) => c.branch === wanted) ?? null;
}

// Every (label, candidate) the ticket continues from.
export function continuedBranches(prior: PriorWork | null | undefined): { label: string; candidate: PriorWorkCandidate }[] {
  return Object.keys(prior?.repos ?? {})
    .sort()
    .flatMap((label) => {
      const candidate = startCandidate(prior, label);
      return candidate ? [{ label, candidate }] : [];
    });
}

// A done ticket's branches, by what became of them in the epic branch.
export function doneWorkOutcome(prior: PriorWork | null | undefined): "merged" | "conflict" | "choose" | "not_done" | null {
  const results = Object.values(prior?.epic_merge ?? {});
  if (!results.length) return null;
  if (results.some((r) => r.result === "conflict")) return "conflict";
  if (results.some((r) => r.result === "choose")) return "choose";
  if (results.some((r) => r.result === "merged" || r.result === "in_epic")) return "merged";
  return "not_done";
}

// Found only work that is already in the default branch.
export function onlyInDefault(prior: PriorWork | null | undefined): boolean {
  const findings = Object.values(prior?.repos ?? {});
  return findings.length > 0 && findings.every((f) => f.verdict === "in_default");
}
