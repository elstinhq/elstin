import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";

// How a project gets a git repo (see forge/services/repo_clone.py): clone a
// URL into storage the app controls — works the same on the host and inside
// `docker compose up` — or attach a repo that already exists on disk, used in
// place. Attaching only works for a path the forge process can see: any path
// when forge runs on the host, and inside Docker only a folder mounted into
// the container (docker-compose.yml).
export type RepoSourceMode = "clone" | "attach";

export interface RepoSource {
  mode: RepoSourceMode;
  value: string;
}

export const EMPTY_REPO_SOURCE: RepoSource = { mode: "clone", value: "" };

const MODES: Record<RepoSourceMode, { label: string; field: string; placeholder: string; hint: string }> = {
  clone: {
    label: "Clone from URL",
    field: "Repository URL",
    placeholder: "https://github.com/org/repo.git",
    hint: "Cloned into the app's own storage — takes a moment for larger repos.",
  },
  attach: {
    label: "Attach a folder",
    field: "Folder path",
    placeholder: "/Users/you/code/app",
    hint:
      "A git repo already on this machine, used in place — or a folder holding several, to pick from. forge never " +
      "changes their checked-out files; it creates its branches and worktrees from them. Running in Docker? Mount " +
      "the folder first (see docker-compose.yml) and give the path inside the container.",
  },
};

interface RepoSourceInputProps {
  source: RepoSource;
  onChange: (source: RepoSource) => void;
  // Shown after the field name, e.g. "(optional)" when creating a project.
  suffix?: string;
}

export function RepoSourceInput({ source, onChange, suffix }: RepoSourceInputProps) {
  const current = MODES[source.mode];
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex gap-2" role="group" aria-label="How to add the repository">
        {(Object.keys(MODES) as RepoSourceMode[]).map((mode) => (
          <Button
            key={mode}
            type="button"
            size="sm"
            variant={mode === source.mode ? "default" : "outline"}
            aria-pressed={mode === source.mode}
            onClick={() => mode !== source.mode && onChange({ mode, value: "" })}
          >
            {MODES[mode].label}
          </Button>
        ))}
      </div>
      <Label htmlFor="repo-source" className="mt-1">
        {current.field}
        {suffix ? <span className="text-muted-foreground font-normal"> {suffix}</span> : null}
      </Label>
      <Input
        id="repo-source"
        placeholder={current.placeholder}
        value={source.value}
        spellCheck={false}
        autoComplete="off"
        onChange={(e) => onChange({ ...source, value: e.target.value })}
      />
      <p className="text-muted-foreground text-xs">{current.hint}</p>
    </div>
  );
}

// The API body for what was entered, or null when nothing was.
export function repoSourceBody(source: RepoSource): { url: string } | { path: string } | null {
  const value = source.value.trim();
  if (!value) return null;
  return source.mode === "clone" ? { url: value } : { path: value };
}
