import { useState } from "react";
import { Plus, X } from "lucide-react";
import { EMPTY_REPO_SOURCE, RepoSourceInput, repoSourceBody, type RepoSource } from "../shared/RepoSourceInput";
import { api } from "../../lib/api";
import { useAddProjectRepo, useProjectRepos, useRemoveProjectRepo } from "../../hooks/useFolders";
import { useToast } from "../../lib/toast";
import type { ProjectRepo } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { RepoGuideControl } from "./RepoGuide";

// No repo is bound to an epic — every repo here is already readable and
// writable by the plan (forge/tools/workspace.py). There is no per-epic
// picker; a new epic gets a default assigned automatically on sync
// (forge/services/epic_sync.py, the project's first-added repo). This
// covers the one case that misses: an epic synced before this project had
// any repo at all, which stays folder-less until the daemon's next sweep
// backfills it — adding a repo here backfills it immediately instead.
async function backfillFolderlessEpics(projectId: number, repoId: number) {
  const epics = await api.listProjectEpics(projectId);
  await Promise.all(epics.filter((epic) => !epic.repo).map((epic) => api.setEpicRepo(epic.jira_key, repoId)));
}

function FolderRow({ projectId, repo }: { projectId: number; repo: ProjectRepo }) {
  const removeRepo = useRemoveProjectRepo(projectId);
  const { showToast } = useToast();

  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{repo.label || repo.path}</div>
        {/* Where it came from: the URL it was cloned from, or — for a
            folder attached in place — where it lives. */}
        <div className="text-muted-foreground truncate text-xs" title={repo.clone_url || repo.path}>
          {repo.clone_url || `attached · ${repo.path}`}
        </div>
        <RepoGuideControl projectId={projectId} repoId={repo.id} />
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="size-7 shrink-0"
        aria-label="Remove folder"
        loading={removeRepo.isPending}
        onClick={() =>
          removeRepo.mutate(repo.id, {
            onError: (err) => showToast(err.message || "Could not remove folder", "error"),
          })
        }
      >
        <X />
      </Button>
    </div>
  );
}

export function FoldersPanel({ projectId }: { projectId: number }) {
  const reposQuery = useProjectRepos(projectId);
  const addRepo = useAddProjectRepo(projectId);
  const { showToast } = useToast();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [source, setSource] = useState<RepoSource>(EMPTY_REPO_SOURCE);
  const repos = reposQuery.data ?? [];
  const attaching = source.mode === "attach";

  function handleAdd() {
    const body = repoSourceBody(source);
    if (!body) {
      showToast(attaching ? "Folder path is required" : "Repository URL is required", "error");
      return;
    }
    addRepo.mutate(body, {
      onSuccess: async (repo) => {
        setDialogOpen(false);
        showToast(attaching ? "Folder attached" : "Repository cloned", "success");
        await backfillFolderlessEpics(projectId, repo.id);
      },
      onError: (err) => showToast(err.message || (attaching ? "Could not attach folder" : "Could not clone repository"), "error"),
    });
  }

  return (
    <>
      <Card className="p-4">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">Folders</span>
            <Button variant="ghost" size="icon" className="size-7" aria-label="Add folder" onClick={() => { setSource(EMPTY_REPO_SOURCE); setDialogOpen(true); }}>
              <Plus />
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Git repos this project can run against. Every repo is already readable and writable by the plan — a ticket
            spanning several repos just works, nothing to pick per ticket. The first repo added here is the default
            for unprefixed paths.
          </p>
          <div className="flex flex-col gap-2">
            {repos.length === 0 ? (
              <p className="text-muted-foreground text-sm">No folders attached yet — use + to add a git repo.</p>
            ) : (
              repos.map((repo) => <FolderRow key={repo.id} projectId={projectId} repo={repo} />)
            )}
          </div>
        </div>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add folder</DialogTitle>
          </DialogHeader>
          <div className="flex flex-col gap-4">
            <p className="text-muted-foreground text-xs">
              Git repos this project can run against. Every repo added here is readable and writable by the plan — a
              ticket spanning several repos just works, it doesn't need one picked per repo.
            </p>
            <RepoSourceInput source={source} onChange={setSource} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button loading={addRepo.isPending} onClick={handleAdd}>
              {attaching ? "Attach" : "Clone"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
