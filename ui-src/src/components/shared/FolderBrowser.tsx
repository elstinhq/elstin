import { useState } from "react";
import { ArrowUp, Folder, FolderGit2, House } from "lucide-react";
import { useBrowseFolder } from "../../hooks/useFolders";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";

// A folder picker that browses the machine ELSTIN runs on. A browser cannot
// hand a page the real path of a folder chosen in the system dialog, and the
// path that matters is the one elstin sees — inside Docker, a mount point, not
// the host's path — so the listing comes from elstin
// (elstin/services/repo_clone.py's browse_folder). Click a folder to open it;
// a git repo can be taken as is with its Use button.
export function FolderBrowser({
  startPath,
  onPick,
  onClose,
}: {
  startPath: string | null;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [path, setPath] = useState<string | null>(startPath);
  const listing = useBrowseFolder(path, true);
  const data = listing.data;

  return (
    <div className="border-border flex flex-col gap-2 rounded-md border p-2" role="group" aria-label="Browse folders">
      <div className="flex items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label="Up one folder"
          disabled={!data?.parent}
          onClick={() => data?.parent && setPath(data.parent)}
        >
          <ArrowUp />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label="Home folder"
          onClick={() => setPath(data?.home ?? null)}
        >
          <House />
        </Button>
        <span className="text-muted-foreground min-w-0 flex-1 truncate font-mono text-xs" title={data?.path ?? path ?? ""} dir="rtl">
          {/* rtl + an ltr mark: a long path is cut at its start, keeping the folder names that matter */}
          &lrm;{data?.path ?? path ?? "…"}
        </span>
      </div>

      <div className="flex max-h-56 min-h-24 flex-col overflow-y-auto">
        {listing.isLoading ? (
          <p className="text-muted-foreground p-2 text-xs">Loading…</p>
        ) : listing.isError ? (
          <div className="flex flex-col items-start gap-2 p-2">
            <p className="text-destructive text-xs">{(listing.error as Error).message}</p>
            <Button type="button" variant="outline" size="sm" onClick={() => setPath(null)}>
              Go to the home folder
            </Button>
          </div>
        ) : data && !data.entries.length ? (
          <p className="text-muted-foreground p-2 text-xs">No folders in here.</p>
        ) : (
          data?.entries.map((entry) => (
            <div key={entry.path} className="hover:bg-accent/50 flex items-center gap-1 rounded">
              <button
                type="button"
                className="flex min-w-0 flex-1 items-center gap-2 px-2 py-1 text-left text-sm"
                onClick={() => setPath(entry.path)}
                title={`Open ${entry.path}`}
              >
                {entry.is_repo ? (
                  <FolderGit2 className="text-primary size-4 shrink-0" />
                ) : (
                  <Folder className="text-muted-foreground size-4 shrink-0" />
                )}
                <span className={cn("truncate", entry.is_repo && "font-medium")}>{entry.name}</span>
                {entry.is_repo ? <span className="text-muted-foreground shrink-0 text-xs">git repo</span> : null}
              </button>
              {entry.is_repo ? (
                <Button type="button" variant="ghost" size="sm" className="h-7 shrink-0 px-2 text-xs" onClick={() => onPick(entry.path)}>
                  Use
                </Button>
              ) : null}
            </div>
          ))
        )}
        {data?.truncated ? <p className="text-muted-foreground p-2 text-xs">Only the first 1000 folders are shown.</p> : null}
      </div>

      <div className="flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
        <Button type="button" size="sm" disabled={!data} onClick={() => data && onPick(data.path)}>
          Use this folder
        </Button>
      </div>
    </div>
  );
}
