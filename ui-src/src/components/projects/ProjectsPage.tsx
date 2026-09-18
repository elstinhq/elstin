import { useState } from "react";
import { FolderOpen, Plus } from "lucide-react";
import { AppShell } from "../shared/AppShell";
import { EmptyState } from "../shared/EmptyState";
import { ProjectCard } from "./ProjectCard";
import { CreateProjectModal } from "./CreateProjectModal";
import { useProjects } from "../../hooks/useProjects";
import { Button } from "../ui/button";

export function ProjectsPage() {
  const projectsQuery = useProjects();
  const [modalOpen, setModalOpen] = useState(false);
  const projects = projectsQuery.data ?? [];

  return (
    <AppShell rightLinkHref="/settings" rightLinkLabel="Settings">
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-xl font-semibold">Projects</h1>
        <Button onClick={() => setModalOpen(true)}>
          <Plus />
          New project
        </Button>
      </div>
      <div className="mt-6 grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-4">
        {projects.length === 0 && projectsQuery.isSuccess ? (
          <div className="col-span-full">
            <EmptyState icon={FolderOpen} heading="No projects yet" body="Create one to get started." />
          </div>
        ) : (
          projects.map((project) => <ProjectCard key={project.id} project={project} />)
        )}
      </div>
      <CreateProjectModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </AppShell>
  );
}
