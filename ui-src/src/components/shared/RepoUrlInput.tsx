import { Input } from "../ui/input";
import { Label } from "../ui/label";

// Shared "Repository URL" input. The API clones whatever URL is submitted
// into storage it controls (see forge/services/repo_clone.py) rather than
// expecting a path it can already see on disk — the same flow works
// whether this runs directly on a dev machine or inside `docker compose
// up`, which has no view of the host filesystem.
interface RepoUrlInputProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
}

export function RepoUrlInput({ value, onChange, label = "Repository URL" }: RepoUrlInputProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="repo-url">{label}</Label>
      <Input
        id="repo-url"
        placeholder="https://github.com/org/repo.git"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      <p className="text-muted-foreground text-xs">Cloned into the app's own storage — takes a moment for larger repos.</p>
    </div>
  );
}

export function normalizeRepoUrl(value: string): string | null {
  return value.trim() || null;
}
