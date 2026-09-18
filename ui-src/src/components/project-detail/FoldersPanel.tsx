import { useState } from "react";
import { Plus, X } from "lucide-react";
import { RepoUrlInput, normalizeRepoUrl } from "../shared/RepoUrlInput";
import { api } from "../../lib/api";
import { useCloneProjectRepo, useProjectRepos, useRemoveProjectRepo } from "../../hooks/useFolders";
import { useToast } from "../../lib/toast";
import type { ProjectRepo } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";

// Multi-repo projects (e.g. Vue admin + Spring API) need an explicit choice
// per epic; only auto-wire when there is nowhere else to point.
async function assignSoleFolderToEpics(projectId: number, repoId: number, repos: ProjectRepo[]) {
  if (repos.length !== 1) return;
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
        {repo.clone_url ? <div className="text-muted-foreground truncate text-xs">{repo.clone_url}</div> : null}
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
  const cloneRepo = useCloneProjectRepo(projectId);
  const { showToast } = useToast();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const repos = reposQuery.data ?? [];

  function handleClone() {
    const url = normalizeRepoUrl(repoUrl);
    if (!url) {
      showToast("Repository URL is required", "error");
      return;
    }
    cloneRepo.mutate(url, {
      onSuccess: async (repo) => {
        setDialogOpen(false);
        showToast("Repository cloned", "success");
        const nextRepos = await api.listProjectRepos(projectId);
        await assignSoleFolderToEpics(projectId, repo.id, nextRepos);
      },
      onError: (err) => showToast(err.message || "Could not clone repository", "error"),
    });
  }

  return (
    <>
      <Card className="p-4">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">Folders</span>
            <Button variant="ghost" size="icon" className="size-7" aria-label="Add folder" onClick={() => { setRepoUrl(""); setDialogOpen(true); }}>
              <Plus />
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Git repos this project can run against. Each task then picks one (auto-selected when there is only one).
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
              Git repos this project can run against. Each task then picks one (auto-selected when there is only one).
            </p>
            <RepoUrlInput value={repoUrl} onChange={setRepoUrl} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button loading={cloneRepo.isPending} onClick={handleClone}>
              Clone
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
