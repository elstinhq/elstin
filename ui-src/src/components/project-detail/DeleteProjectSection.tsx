import { useState } from "react";
import { Trash2 } from "lucide-react";
import { useProjectEpics } from "../../hooks/useEpics";
import { useProjectRepos } from "../../hooks/useFolders";
import { useDeleteProject, useProject } from "../../hooks/useProjects";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";

// Deleting a project removes everything forge holds for it — each ticket as
// Remove ticket does, the clones forge made, its settings, filter and folders
// (forge/daemon/project_removal.py). It can't be undone, so the confirmation
// spells out what goes and what stays, and asks for the project's name.
export function DeleteProjectSection({ projectId }: { projectId: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-destructive/40 flex items-center justify-between gap-3 rounded-md border p-3">
      <div className="min-w-0">
        <div className="text-sm font-medium">Delete this project</div>
        <p className="text-muted-foreground text-xs">
          Removes its tickets, run history and forge's clones. Attached folders and anything on GitHub or JIRA stay.
        </p>
      </div>
      <Button variant="destructive" size="sm" className="shrink-0" onClick={() => setOpen(true)}>
        <Trash2 />
        Delete
      </Button>
      {open ? <DeleteProjectDialog projectId={projectId} onClose={() => setOpen(false)} /> : null}
    </div>
  );
}

function DeleteProjectDialog({ projectId, onClose }: { projectId: number; onClose: () => void }) {
  const project = useProject(projectId).data;
  const repos = useProjectRepos(projectId).data ?? [];
  const epics = useProjectEpics(projectId).data ?? [];
  const deleteProject = useDeleteProject(projectId);
  const { showToast } = useToast();
  const [typed, setTyped] = useState("");

  const name = project?.name ?? "";
  const running = epics.flatMap((e) => e.stories ?? []).filter((s) => s.status === "dispatched");
  const clones = repos.filter((r) => r.clone_url);
  const attached = repos.filter((r) => !r.clone_url);
  const pullRequests = epics.flatMap((e) => e.pull_requests ?? []).filter((pr) => pr.number);

  function handleDelete() {
    deleteProject.mutate(undefined, {
      onSuccess: (report) => {
        showToast(`Deleted ${report.name}`, "success");
        if (report.warnings.length) {
          showToast(`Deleted, but ${report.warnings.length} clean-up step(s) had a problem: ${report.warnings[0]}`, "warning");
        }
        window.location.href = "/";
      },
      onError: (err) => showToast(err.message || "Could not delete this project", "error"),
    });
  }

  return (
    <Dialog open onOpenChange={(next) => !next && !deleteProject.isPending && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {name || "this project"}?</DialogTitle>
          <DialogDescription>This can't be undone.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 text-sm">
          <div>
            <p className="font-medium">Deleted from forge</p>
            <ul className="text-muted-foreground list-disc pl-5">
              <li>
                {epics.length} ticket{epics.length === 1 ? "" : "s"} with their run history, token usage, worktrees, local
                branches and saved run state
              </li>
              {clones.length ? (
                <li>
                  The clone{clones.length === 1 ? "" : "s"} forge made: {clones.map((r) => r.label || r.path).join(", ")}
                </li>
              ) : null}
              <li>The project's settings, ticket filter and folder list</li>
            </ul>
          </div>
          <div>
            <p className="font-medium">Left as they are</p>
            <ul className="text-muted-foreground list-disc pl-5">
              {attached.length ? (
                <li>
                  Attached folder{attached.length === 1 ? "" : "s"} ({attached.map((r) => r.path).join(", ")}) — only the
                  branches and worktrees forge added are removed
                </li>
              ) : null}
              <li>
                Everything on GitHub and JIRA
                {pullRequests.length ? ` — including pull request${pullRequests.length === 1 ? "" : "s"} ${pullRequests.map((pr) => `#${pr.number}`).join(", ")}` : ""}
              </li>
            </ul>
          </div>
          {running.length ? (
            <p className="text-destructive text-xs">
              {running.map((s) => s.jira_key).join(", ")} {running.length === 1 ? "is" : "are"} running — cancel or wait for
              {running.length === 1 ? " it" : " them"} first.
            </p>
          ) : null}
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="confirm-project-name">
              Type <span className="font-mono">{name}</span> to confirm
            </Label>
            <Input
              id="confirm-project-name"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setTyped(e.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" disabled={deleteProject.isPending} onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            disabled={!name || typed.trim() !== name || running.length > 0}
            loading={deleteProject.isPending}
            onClick={handleDelete}
          >
            Delete project
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
