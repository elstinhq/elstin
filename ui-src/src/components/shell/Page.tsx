import { Fragment, useEffect, type ReactNode } from "react";
import { ChevronRight, Menu } from "lucide-react";
import { Link } from "../../lib/router";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { useShell } from "./ShellContext";

export interface Crumb {
  label: string;
  href?: string;
}

interface PageProps {
  crumbs: Crumb[];
  actions?: ReactNode;
  // The browser tab's title; defaults to the last crumb.
  title?: string;
  // A view that manages its own height and scrolling (the backlog draft's two
  // panes) asks for no padding and no max width.
  bleed?: boolean;
  children: ReactNode;
}

// A view's frame: the top bar (breadcrumb on the left, the view's actions on
// the right) and the content under it.
export function Page({ crumbs, actions, title, bleed = false, children }: PageProps) {
  const { openNav } = useShell();
  const tabTitle = title ?? crumbs[crumbs.length - 1]?.label;

  useEffect(() => {
    document.title = tabTitle ? `${tabTitle} — Elstin` : "Elstin";
  }, [tabTitle]);

  return (
    <>
      <header className="bg-background/95 supports-[backdrop-filter]:bg-background/80 sticky top-0 z-30 flex min-h-16 flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3 backdrop-blur md:px-8">
        <Button variant="ghost" size="icon" className="-ml-2 md:hidden" onClick={openNav} aria-label="Open navigation">
          <Menu />
        </Button>
        <nav aria-label="Breadcrumb" className="min-w-0 flex-1">
          <ol className="flex min-w-0 items-center gap-1.5 text-sm">
            {crumbs.map((crumb, i) => {
              const last = i === crumbs.length - 1;
              return (
                <Fragment key={`${crumb.label}-${i}`}>
                  {i > 0 ? <ChevronRight className="text-muted-foreground size-4 shrink-0" aria-hidden="true" /> : null}
                  <li className={cn("min-w-0 truncate", last ? "text-foreground font-medium" : "text-muted-foreground")}>
                    {crumb.href && !last ? (
                      <Link href={crumb.href} className="hover:text-foreground transition-colors">
                        {crumb.label}
                      </Link>
                    ) : (
                      <span aria-current={last ? "page" : undefined}>{crumb.label}</span>
                    )}
                  </li>
                </Fragment>
              );
            })}
          </ol>
        </nav>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </header>
      <main className={cn("flex-1", bleed ? "flex min-h-0 flex-col" : "mx-auto w-full max-w-[1400px] px-4 py-6 md:px-8 md:py-8")}>
        {children}
      </main>
    </>
  );
}

// A heading with an optional muted note beside it ("Tasks  Refreshes every 5s").
export function SectionHeading({ title, note, children }: { title: string; note?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h2 className="shrink-0 text-lg font-semibold tracking-tight whitespace-nowrap">{title}</h2>
        {note ? <span className="text-muted-foreground min-w-0 text-sm">{note}</span> : null}
      </div>
      {children}
    </div>
  );
}
