import { GitPullRequest } from "lucide-react";
import { useDecideEpicPr } from "../../hooks/useEpics";
import { useToast } from "../../lib/toast";
import type { Epic } from "../../types/api";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";

// With "Hold the epic's pull request for approval" on, an epic whose stories have all
// merged pushes nothing and opens nothing until someone says so. The branch is ready
// locally either way: "Open" pushes it and opens the pull request, "Keep local" ends the
// epic without one.
export function PrApprovalPrompt({ projectId, epic }: { projectId: number; epic: Epic }) {
  const decide = useDecideEpicPr(projectId);
  const { showToast } = useToast();
  if (epic.pr_approval !== "awaiting") return null;
  const branch = epic.integration_branch || "the epic branch";

  function choose(action: "open" | "skip") {
    decide.mutate(
      { epicKey: epic.jira_key, action },
      {
        onSuccess: () =>
          showToast(action === "open" ? `Opening the pull request for ${epic.jira_key}` : `${epic.jira_key} kept local — no pull request`, "success"),
        onError: (err) => showToast(err.message || "Could not record this decision", "error"),
      },
    );
  }

  return (
    <Alert variant="warning">
      <GitPullRequest />
      <AlertTitle>Every story has merged — waiting for you to open the pull request</AlertTitle>
      <AlertDescription>
        <p>
          <span className="font-mono">{branch}</span> is ready locally. Nothing has been pushed to GitHub.
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" loading={decide.isPending && decide.variables?.action === "open"} disabled={decide.isPending} onClick={() => choose("open")}>
            Open pull request
          </Button>
          <Button
            size="sm"
            variant="outline"
            loading={decide.isPending && decide.variables?.action === "skip"}
            disabled={decide.isPending}
            onClick={() => choose("skip")}
            title="End the epic with its branch kept on this machine; no pull request is opened"
          >
            Keep local — no pull request
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}
