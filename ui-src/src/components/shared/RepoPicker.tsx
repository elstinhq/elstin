import type { ScanFolderResult } from "../../types/api";
import { cn } from "../../lib/utils";

// A folder that is not a git repo but holds several (one checkout per
// service), like ~/dev/mono: forge works inside one repository at a time — a
// branch and a worktree per story, local merges, the epic's pull request — so
// each repo picked here is attached as a folder of its own
// (forge/services/repo_clone.py's scan_folder). Every folder a project has is
// read while planning, so this asks rather than taking them all.
//
// Two more choices per pick. A repo can be attached read-only — read by every
// run, never in a plan (a shared contract repo, say). And the folder itself,
// minus its repos and data files, can be read for context: its CLAUDE.md about
// how the repos relate, its docs (forge/tools/reference_folder.py).

export interface RepoSelection {
  picked: string[];
  readOnly: string[];
  reference: boolean;
}

// The repos that can be picked: attachable, and not attached already.
export function attachableRepoPaths(scan: ScanFolderResult): string[] {
  return scan.repos.filter((r) => !r.problem && !r.attached).map((r) => r.path);
}

// Nothing is pre-selected — a workspace folder often holds repos a project
// has nothing to do with — unless there is only one to pick. The folder's own
// context is, when it has any: reading notes costs little and is why it is there.
export function initialSelection(scan: ScanFolderResult): RepoSelection {
  const selectable = attachableRepoPaths(scan);
  return {
    picked: selectable.length === 1 ? selectable : [],
    readOnly: [],
    reference: Boolean(scan.reference?.readable.length),
  };
}

// How many folders the selection attaches.
export function selectionCount(selection: RepoSelection): number {
  return selection.picked.length + (selection.reference ? 1 : 0);
}

export function RepoPicker({
  scan,
  selection,
  onChange,
}: {
  scan: ScanFolderResult;
  selection: RepoSelection;
  onChange: (selection: RepoSelection) => void;
}) {
  const selectable = attachableRepoPaths(scan);
  const { picked, readOnly } = selection;
  const toggle = (path: string) =>
    onChange(
      picked.includes(path)
        ? { ...selection, picked: picked.filter((p) => p !== path), readOnly: readOnly.filter((p) => p !== path) }
        : { ...selection, picked: [...picked, path] },
    );
  const toggleReadOnly = (path: string) =>
    onChange({ ...selection, readOnly: readOnly.includes(path) ? readOnly.filter((p) => p !== path) : [...readOnly, path] });
  const reference = scan.reference;
  const folderName = scan.path.split("/").filter(Boolean).pop() ?? scan.path;

  return (
    <div className="flex flex-col gap-2">
      <p className="text-muted-foreground text-xs">
        This folder isn't a git repo itself, but it holds {scan.repos.length}. forge works inside one repo at a time, so
        each one you pick becomes a folder of this project. Pick only what the tickets touch — every folder is read while
        planning. Mark a repo read-only to have it read but never changed.
      </p>
      <div className="border-border flex max-h-60 flex-col gap-0.5 overflow-y-auto rounded-md border p-1.5" role="group" aria-label="Repos in this folder">
        {scan.repos.map((repo) => {
          const disabled = Boolean(repo.problem) || repo.attached;
          const isPicked = picked.includes(repo.path);
          return (
            <div key={repo.path} className="flex items-start justify-between gap-2" title={repo.path}>
              <label
                className={cn(
                  "flex min-w-0 flex-1 items-start gap-2 rounded px-1.5 py-1 text-sm",
                  disabled ? "opacity-60" : "hover:bg-accent/50 cursor-pointer",
                )}
              >
                <input
                  type="checkbox"
                  className="accent-primary mt-0.5"
                  checked={isPicked}
                  disabled={disabled}
                  onChange={() => toggle(repo.path)}
                />
                <span className="min-w-0">
                  <span className="font-medium">{repo.name}</span>
                  {repo.attached ? <span className="text-muted-foreground text-xs"> · already attached</span> : null}
                  {repo.problem ? <span className="text-destructive block text-xs">{repo.problem}</span> : null}
                </span>
              </label>
              {isPicked ? (
                <label className="text-muted-foreground flex shrink-0 cursor-pointer items-center gap-1.5 px-1.5 py-1 text-xs">
                  <input
                    type="checkbox"
                    className="accent-primary"
                    checked={readOnly.includes(repo.path)}
                    onChange={() => toggleReadOnly(repo.path)}
                    aria-label={`${repo.name} read-only`}
                  />
                  read-only
                </label>
              ) : null}
            </div>
          );
        })}
      </div>
      {selectable.length > 1 ? (
        <div className="flex gap-3 text-xs">
          <button type="button" className="text-primary underline-offset-2 hover:underline" onClick={() => onChange({ ...selection, picked: selectable })}>
            Select all
          </button>
          <button type="button" className="text-primary underline-offset-2 hover:underline" onClick={() => onChange({ ...selection, picked: [], readOnly: [] })}>
            Select none
          </button>
        </div>
      ) : null}
      {reference && (reference.readable.length || reference.skipped.length) ? (
        <label className="border-border hover:bg-accent/50 flex cursor-pointer items-start gap-2 rounded-md border p-2 text-sm">
          <input
            type="checkbox"
            className="accent-primary mt-0.5"
            checked={selection.reference}
            disabled={!reference.readable.length}
            onChange={() => onChange({ ...selection, reference: !selection.reference })}
          />
          <span className="min-w-0">
            <span className="font-medium">Also read {folderName} itself, for context</span>
            <span className="text-muted-foreground block text-xs">
              {reference.readable.length
                ? `Read-only, never changed: ${reference.readable.join(", ")}.`
                : "Nothing in it to read besides the repos."}
              {reference.skipped.length
                ? ` Left out: ${reference.skipped.map((s) => `${s.name} (${s.reason})`).join(", ")}.`
                : ""}
            </span>
          </span>
        </label>
      ) : null}
    </div>
  );
}
