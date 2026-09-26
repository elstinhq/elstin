import { useEffect, useState } from "react";
import { EMPTY_REPO_SOURCE, RepoSourceInput, repoSourceBody, type RepoSource } from "../shared/RepoSourceInput";
import { useCreateProject } from "../../hooks/useProjects";
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
  const createProject = useCreateProject();
  const { showToast } = useToast();

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setRepoSource(EMPTY_REPO_SOURCE);
    }
  }, [open]);

  function handleSubmit() {
    const trimmedName = name.trim();
    if (!trimmedName) {
      showToast("Project name is required", "error");
      return;
    }
    const repo = repoSourceBody(repoSource);
    createProject.mutate(
      {
        name: trimmedName,
        description: description.trim(),
        repo_url: repo && "url" in repo ? repo.url : null,
        repo_path: repo && "path" in repo ? repo.path : null,
      },
      {
        onSuccess: (project) => {
          window.location.href = `/projects/${project.id}`;
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
          <RepoSourceInput source={repoSource} onChange={setRepoSource} suffix="(optional)" />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button loading={createProject.isPending} onClick={handleSubmit}>
            Create project
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
