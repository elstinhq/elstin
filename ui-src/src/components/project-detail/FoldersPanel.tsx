import { useState } from "react";
import { Plus, X } from "lucide-react";
import { EMPTY_REPO_SOURCE, RepoSourceInput, repoSourceBody, type RepoSource } from "../shared/RepoSourceInput";
import { RepoPicker, initialSelection, selectionCount, type RepoSelection } from "../shared/RepoPicker";
import { Tag } from "../shared/StatusTag";
import { api } from "../../lib/api";
import { useAddProjectRepo, useProjectRepos, useRemoveProjectRepo, useSetRepoReadOnly } from "../../hooks/useFolders";
import { useToast } from "../../lib/toast";
import type { ProjectRepo, ScanFolderResult } from "../../types/api";
import { Button } from "../ui/button";
import { Card } from "../ui/card";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { RepoGuideControl } from "./RepoGuide";

// No repo is bound to an epic — every repo here is already readable and
// writable by the plan (elstin/tools/workspace.py). There is no per-epic
// picker; a new epic gets a default assigned automatically on sync
// (elstin/services/epic_sync.py, the project's first-added repo). This
// covers the one case that misses: an epic synced before this project had
// any repo at all, which stays folder-less until the daemon's next sweep
// backfills it — adding a repo here backfills it immediately instead.
async function backfillFolderlessEpics(projectId: number, repoId: number) {
  const epics = await api.listProjectEpics(projectId);
  await Promise.all(epics.filter((epic) => !epic.repo).map((epic) => api.setEpicRepo(epic.jira_key, repoId)));
}

function FolderRow({ projectId, repo, others }: { projectId: number; repo: ProjectRepo; others: ProjectRepo[] }) {
  const removeRepo = useRemoveProjectRepo(projectId);
  const setReadOnly = useSetRepoReadOnly(projectId);
  const { showToast } = useToast();
  const reference = repo.kind === "reference";

  function toggleReadOnly() {
    setReadOnly.mutate(
      { repoId: repo.id, readOnly: !repo.read_only },
      {
        onSuccess: ({ read_only, epics_moved, moved_to }) => {
          const next = others.find((r) => r.id === moved_to);
          const moved = epics_moved.length
            ? ` — ${epics_moved.join(", ")} now ${epics_moved.length === 1 ? "defaults" : "default"} to ${next ? next.label || next.path : "no folder until you add a writable one"}`
            : "";
          showToast(`${repo.label || repo.path} is ${read_only ? "read-only" : "writable"}${moved}`, "success");
        },
        onError: (err) => showToast(err.message || "Could not change it", "error"),
      },
    );
  }

  return (
    <div className="flex items-center justify-between gap-3">
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{repo.label || repo.path}</span>
          {reference ? (
            <Tag label="reference" appearance="info" title="Read for context — its CLAUDE.md, docs — never written. Repos and data files inside it are left out." />
          ) : repo.read_only ? (
            <Tag label="read-only" appearance="secondary" title="Read by every run; no plan may change it" />
          ) : null}
        </div>
        {/* Where it came from: the URL it was cloned from, or — for a
            folder attached in place — where it lives. */}
        <div className="text-muted-foreground truncate text-xs" title={repo.clone_url || repo.path}>
          {repo.clone_url || `attached · ${repo.path}`}
        </div>
        <div className="flex flex-wrap items-center gap-x-2">
          <RepoGuideControl projectId={projectId} repoId={repo.id} canGenerate={!reference} />
          {!reference ? (
            <Button
              variant="link"
              size="sm"
              className="text-muted-foreground h-auto px-0 py-0 text-xs"
              loading={setReadOnly.isPending}
              title={repo.read_only ? "Let plans change this repo again" : "Read by every run, never changed by a plan"}
              onClick={toggleReadOnly}
            >
              {repo.read_only ? "Make writable" : "Make read-only"}
            </Button>
          ) : null}
        </div>
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="size-7 shrink-0"
        aria-label="Remove folder"
        loading={removeRepo.isPending}
        onClick={() =>
          removeRepo.mutate(repo.id, {
            onSuccess: ({ epics_moved, moved_to }) => {
              const next = others.find((r) => r.id === moved_to);
              if (!epics_moved.length) showToast("Folder removed", "success");
              else if (next)
                showToast(`Folder removed — ${epics_moved.join(", ")} now ${epics_moved.length === 1 ? "defaults" : "default"} to ${next.label || next.path}`, "success");
              else
                showToast(`Folder removed — ${epics_moved.join(", ")} ${epics_moved.length === 1 ? "waits" : "wait"} until you add a folder`, "warning");
            },
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
  // Set when the folder to attach turned out to hold repos rather than be
  // one: the dialog then asks which of them to attach.
  const [scan, setScan] = useState<ScanFolderResult | null>(null);
  const [selection, setSelection] = useState<RepoSelection>({ picked: [], readOnly: [], reference: false });
  const [scanning, setScanning] = useState(false);
  const repos = reposQuery.data ?? [];
  const attaching = source.mode === "attach";

  function openDialog() {
    setSource(EMPTY_REPO_SOURCE);
    setScan(null);
    setDialogOpen(true);
  }

  function addOne(body: { url: string } | { path: string }) {
    addRepo.mutate(body, {
      onSuccess: async (repo) => {
        setDialogOpen(false);
        showToast(attaching ? "Folder attached" : "Repository cloned", "success");
        await backfillFolderlessEpics(projectId, repo.id);
      },
      onError: (err) => showToast(err.message || (attaching ? "Could not attach folder" : "Could not clone repository"), "error"),
    });
  }

  async function handleAdd() {
    if (scan) {
      await attachPicked();
      return;
    }
    const body = repoSourceBody(source);
    if (!body) {
      showToast(attaching ? "Folder path is required" : "Repository URL is required", "error");
      return;
    }
    if ("url" in body) {
      addOne(body);
      return;
    }
    // A folder: a repo is attached as it is; one holding repos asks which.
    setScanning(true);
    try {
      const result = await api.scanFolder(body.path, projectId);
      if (result.is_repo) {
        addOne({ path: result.path });
      } else if (!result.repos.length) {
        showToast(`${result.path} is not a git repo and holds none directly inside it`, "error");
      } else {
        setScan(result);
        setSelection(initialSelection(result));
      }
    } catch (err) {
      showToast((err as Error).message || "Could not read that folder", "error");
    } finally {
      setScanning(false);
    }
  }

  async function attachPicked() {
    if (!scan) return;
    const added: ProjectRepo[] = [];
    const failed: string[] = [];
    const bodies = [
      ...selection.picked.map((path) => ({ path, read_only: selection.readOnly.includes(path) })),
      ...(selection.reference ? [{ path: scan.path, kind: "reference" as const }] : []),
    ];
    setScanning(true);
    for (const body of bodies) {
      try {
        added.push(await addRepo.mutateAsync(body));
      } catch (err) {
        failed.push(`${body.path.split("/").pop()}: ${(err as Error).message}`);
      }
    }
    setScanning(false);
    // An epic defaults to a folder a story can write.
    const writable = added.find((r) => r.kind === "repo" && !r.read_only);
    if (writable) await backfillFolderlessEpics(projectId, writable.id);
    if (failed.length) {
      showToast(`Attached ${added.length} of ${bodies.length}. ${failed[0]}`, "error");
      return;
    }
    setDialogOpen(false);
    showToast(`Attached ${added.length} folder${added.length === 1 ? "" : "s"}`, "success");
  }

  return (
    <>
      <Card className="p-4">
        <div className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">Folders</span>
            <Button variant="ghost" size="icon" className="size-7" aria-label="Add folder" onClick={openDialog}>
              <Plus />
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">
            Git repos this project can run against. Every repo is readable and — unless marked read-only — writable by
            the plan: a ticket spanning several repos just works, nothing to pick per ticket. The first writable repo
            added here is the default for unprefixed paths. A reference folder is read for context, never written.
          </p>
          <div className="flex flex-col gap-2">
            {repos.length === 0 ? (
              <p className="text-muted-foreground text-sm">No folders attached yet — use + to add a git repo.</p>
            ) : (
              repos.map((repo) => (
                <FolderRow key={repo.id} projectId={projectId} repo={repo} others={repos.filter((r) => r.id !== repo.id)} />
              ))
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
            <RepoSourceInput
              source={source}
              onChange={(next) => {
                setSource(next);
                setScan(null); // a different folder: look again
              }}
            />
            {scan ? <RepoPicker scan={scan} selection={selection} onChange={setSelection} /> : null}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button loading={addRepo.isPending || scanning} disabled={Boolean(scan) && !selectionCount(selection)} onClick={handleAdd}>
              {scan
                ? `Attach ${selectionCount(selection)} folder${selectionCount(selection) === 1 ? "" : "s"}`
                : attaching
                  ? "Attach"
                  : "Clone"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
