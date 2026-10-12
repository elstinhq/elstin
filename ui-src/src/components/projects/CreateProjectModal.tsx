import { useEffect, useState } from "react";
import { EMPTY_REPO_SOURCE, RepoSourceInput, repoSourceBody, type RepoSource } from "../shared/RepoSourceInput";
import { RepoPicker, initialSelection, selectionCount, type RepoSelection } from "../shared/RepoPicker";
import { useCreateProject } from "../../hooks/useProjects";
import { api } from "../../lib/api";
import { navigate } from "../../lib/router";
import type { ScanFolderResult } from "../../types/api";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Textarea } from "../ui/textarea";

interface CreateProjectModalProps {
  open: boolean;
  onClose: () => void;
}

export function CreateProjectModal({ open, onClose }: CreateProjectModalProps) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [repoSource, setRepoSource] = useState<RepoSource>(EMPTY_REPO_SOURCE);
  // Set when the folder to attach holds repos rather than being one.
  const [scan, setScan] = useState<ScanFolderResult | null>(null);
  const [selection, setSelection] = useState<RepoSelection>({ picked: [], readOnly: [], reference: false });
  const [scanning, setScanning] = useState(false);
  const createProject = useCreateProject();
  const { showToast } = useToast();

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setRepoSource(EMPTY_REPO_SOURCE);
      setScan(null);
    }
  }, [open]);

  async function handleSubmit() {
    const trimmedName = name.trim();
    if (!trimmedName) {
      showToast("Project name is required", "error");
      return;
    }
    const repo = repoSourceBody(repoSource);
    let repoPaths: string[] = [];
    let readOnlyPaths: string[] = [];
    let referencePath: string | null = null;
    if (scan) {
      repoPaths = selection.picked;
      readOnlyPaths = selection.readOnly;
      referencePath = selection.reference ? scan.path : null;
    } else if (repo && "path" in repo) {
      // A folder that holds repos rather than being one: ask which first.
      setScanning(true);
      try {
        const result = await api.scanFolder(repo.path);
        if (!result.is_repo) {
          if (!result.repos.length) showToast(`${result.path} is not a git repo and holds none directly inside it`, "error");
          else {
            setScan(result);
            setSelection(initialSelection(result));
          }
          return;
        }
        repoPaths = [result.path];
      } catch (err) {
        showToast((err as Error).message || "Could not read that folder", "error");
        return;
      } finally {
        setScanning(false);
      }
    }
    createProject.mutate(
      {
        name: trimmedName,
        description: description.trim(),
        repo_url: repo && "url" in repo ? repo.url : null,
        repo_paths: repoPaths,
        read_only_paths: readOnlyPaths,
        reference_path: referencePath,
      },
      {
        onSuccess: (project) => {
          navigate(`/projects/${project.id}/tasks`);
          onClose();
        },
        onError: (err) => showToast(err.message || "Create failed", "error"),
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create a project</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="project-name">What are you working on?</Label>
            <Input id="project-name" placeholder="Name your project" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="project-description">What are you trying to achieve?</Label>
            <Textarea
              id="project-description"
              placeholder="Describe your project, goals, subject, etc..."
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <RepoSourceInput
            source={repoSource}
            onChange={(next) => {
              setRepoSource(next);
              setScan(null); // a different folder: look again
            }}
            suffix="(optional)"
          />
          {scan ? <RepoPicker scan={scan} selection={selection} onChange={setSelection} /> : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={createProject.isPending || scanning} disabled={Boolean(scan) && !selectionCount(selection)} onClick={handleSubmit}>
            {scan
              ? `Create with ${selectionCount(selection)} folder${selectionCount(selection) === 1 ? "" : "s"}`
              : "Create project"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
