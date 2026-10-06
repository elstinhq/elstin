import type { ReactNode } from "react";
import { Button } from "../ui/button";
import { ThemeToggle } from "./ThemeToggle";

interface AppShellProps {
  rightLinkHref: string;
  rightLinkLabel: string;
  children: ReactNode;
}

export function AppShell({ rightLinkHref, rightLinkLabel, children }: AppShellProps) {
  return (
    <>
      <a
        href="#main-content"
        className="bg-background focus:ring-ring absolute left-2 top-2 z-50 -translate-y-16 rounded-md px-4 py-2 text-sm font-medium shadow-md focus:translate-y-0 focus:outline-none focus:ring-2"
      >
        Skip to content
      </a>
      <div className="flex min-h-screen flex-col">
        <header className="bg-background flex items-center justify-between gap-4 border-b px-6 py-3">
          <a href="/projects" className="text-base font-semibold">
            <span className="hidden sm:inline">Elstin</span>
            <span className="sm:hidden">E</span>
          </a>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" asChild>
              <a href={rightLinkHref}>{rightLinkLabel}</a>
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <main id="main-content" className="mx-auto w-full max-w-[1600px] flex-1 p-6">
          {children}
        </main>
      </div>
    </>
  );
}
