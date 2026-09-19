// Where a project's tickets come from, and the words and links that go with
// it. The server owns the list of sources it can actually talk to and refuses
// any other value, so this is only the labels for the ones the UI knows.

export type TicketSourceName = "jira" | "github";

export const TICKET_SOURCES: { value: TicketSourceName; label: string }[] = [
  { value: "jira", label: "JIRA" },
  { value: "github", label: "GitHub Issues" },
];

export function sourceLabel(source: string | null | undefined): string {
  return TICKET_SOURCES.find((s) => s.value === source)?.label ?? "JIRA";
}

export function keyLabel(source: string | null | undefined): string {
  return source === "github" ? "Issue" : "JIRA key";
}

export function keyPlaceholder(source: string | null | undefined): string {
  return source === "github" ? "owner/repo#123 or the issue URL" : "ENG-1";
}

// The ticket's page in its own system, or null when it cannot be worked out
// (a native ticket, or JIRA with no base URL configured).
export function ticketUrl(
  source: string | null | undefined,
  externalId: string | null | undefined,
  key: string,
  jiraBaseUrl: string | null,
): string | null {
  if (source === "github" && externalId) {
    const match = /^([^/]+)\/([^#]+)#(\d+)$/.exec(externalId);
    return match ? `https://github.com/${match[1]}/${match[2]}/issues/${match[3]}` : null;
  }
  if ((source === "jira" || source == null) && jiraBaseUrl) {
    return `${jiraBaseUrl.replace(/\/$/, "")}/browse/${encodeURIComponent(externalId || key)}`;
  }
  return null;
}

// What a row shows for a ticket. A GitHub board key (`gh.owner.repo.12`) is
// built to be path-safe, not to be read, so rows show the issue's own
// `owner/repo#12` — the full reference, since one project can span repos.
export function displayKey(ticket: { jira_key: string; external_source?: string | null; external_id?: string | null }): string {
  return ticket.external_source === "github" && ticket.external_id ? ticket.external_id : ticket.jira_key;
}
