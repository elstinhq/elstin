import type { Project } from "../../types/api";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";

export function ProjectCard({ project }: { project: Project }) {
  return (
    <Card
      className="hover:border-foreground/30 cursor-pointer transition-colors"
      role="button"
      tabIndex={0}
      onClick={() => {
        window.location.href = `/projects/${project.id}`;
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          window.location.href = `/projects/${project.id}`;
        }
      }}
    >
      <CardHeader>
        <CardTitle>{project.name}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-1">
        <p className="text-muted-foreground text-sm">{project.description || ""}</p>
        <p className="text-muted-foreground text-sm">
          {project.repo_count} folder{project.repo_count === 1 ? "" : "s"} · {project.epic_count} task
          {project.epic_count === 1 ? "" : "s"}
        </p>
      </CardContent>
    </Card>
  );
}
