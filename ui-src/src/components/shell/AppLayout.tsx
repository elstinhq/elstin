import { useState, type ReactNode } from "react";
import { useLocation } from "../../lib/router";
import { Sheet, SheetContent, SheetTitle } from "../ui/sheet";
import { ShellContext } from "./ShellContext";
import { Sidebar } from "./Sidebar";

// The frame every view renders in: the sidebar beside the page on a wide
// screen, behind a menu button (TopBar's) on a narrow one.
export function AppLayout({ children }: { children: ReactNode }) {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  // Following a link in the narrow-screen sidebar closes it.
  const [lastPath, setLastPath] = useState(location.pathname);
  if (lastPath !== location.pathname) {
    setLastPath(location.pathname);
    if (navOpen) setNavOpen(false);
  }

  return (
    <ShellContext.Provider value={{ openNav: () => setNavOpen(true) }}>
      <a
        href="#main-content"
        className="bg-background focus:ring-ring absolute top-2 left-2 z-50 -translate-y-16 rounded-md px-4 py-2 text-sm font-medium shadow-md focus:translate-y-0 focus:ring-2 focus:outline-none"
      >
        Skip to content
      </a>
      <div className="flex min-h-screen">
        <aside className="bg-sidebar sticky top-0 hidden h-screen w-64 shrink-0 border-r md:block">
          <Sidebar />
        </aside>
        <Sheet open={navOpen} onOpenChange={setNavOpen}>
          <SheetContent side="left" className="bg-sidebar p-0" showCloseButton={false}>
            <SheetTitle className="sr-only">Navigation</SheetTitle>
            <Sidebar />
          </SheetContent>
        </Sheet>
        <div id="main-content" className="flex min-w-0 flex-1 flex-col">
          {children}
        </div>
      </div>
    </ShellContext.Provider>
  );
}
