/** The app's client-side routing — small on purpose.
 *
 * FastAPI serves one shell (settings-ui/app.html) for every page URL
 * (elstin/app.py), and App.tsx picks the view from `window.location`. This is
 * all a router has to do here: read the location, change it with the history
 * API, and re-render on back/forward. A dependency would bring nested routes,
 * loaders and data APIs this app has no use for.
 *
 * <Link> renders a real <a href>, so a link still works as a link: cmd/ctrl-
 * click, middle-click and "open in new tab" get the browser's own behaviour,
 * and only a plain left click is turned into an in-app navigation.
 */
import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from "react";

const NAVIGATE_EVENT = "elstin:navigate";

function subscribe(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATE_EVENT, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATE_EVENT, listener);
  };
}

// The snapshot must be a stable primitive (useSyncExternalStore compares it
// with Object.is), so the location is read as one string and parsed by callers.
function snapshot(): string {
  return window.location.pathname + window.location.search + window.location.hash;
}

export interface Location {
  pathname: string;
  search: URLSearchParams;
  hash: string;
}

export function useLocation(): Location {
  const href = useSyncExternalStore(subscribe, snapshot, () => "/");
  const url = new URL(href, window.location.origin);
  return { pathname: url.pathname, search: url.searchParams, hash: url.hash };
}

export function navigate(to: string, { replace = false }: { replace?: boolean } = {}): void {
  if (to === snapshot()) return;
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  window.dispatchEvent(new Event(NAVIGATE_EVENT));
  // A new page starts at the top; a change of query (opening the drawer) or
  // of hash does not move the page under the person.
  if (!replace && new URL(to, window.location.origin).pathname !== window.location.pathname) window.scrollTo(0, 0);
}

/** The current URL with `params` set (a null removes one) — for state that
 * lives in the query string, such as which story the drawer shows. */
export function withSearch(params: Record<string, string | null>): string {
  const url = new URL(window.location.href);
  for (const [name, value] of Object.entries(params)) {
    if (value === null) url.searchParams.delete(name);
    else url.searchParams.set(name, value);
  }
  return url.pathname + url.search + url.hash;
}

/** `pattern` is a path like `/projects/:id/backlog/:sid`; returns the named
 * segments when `pathname` matches it exactly, else null. */
export function matchPath(pattern: string, pathname: string): Record<string, string> | null {
  const want = pattern.split("/").filter(Boolean);
  const got = pathname.split("/").filter(Boolean);
  if (want.length !== got.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < want.length; i++) {
    const part = want[i]!;
    if (part.startsWith(":")) params[part.slice(1)] = decodeURIComponent(got[i]!);
    else if (part !== got[i]) return null;
  }
  return params;
}

function isPlainLeftClick(event: MouseEvent<HTMLAnchorElement>): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey && !event.defaultPrevented;
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; replace?: boolean };

export function Link({ href, replace, onClick, target, ...props }: LinkProps) {
  return (
    <a
      href={href}
      target={target}
      onClick={(event) => {
        onClick?.(event);
        if (target || !isPlainLeftClick(event)) return;
        event.preventDefault();
        navigate(href, { replace });
      }}
      {...props}
    />
  );
}
