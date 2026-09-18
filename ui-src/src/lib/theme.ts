/** Light/dark preference, stored per browser.
 *
 * globals.css defines the dark palette twice — once under
 * `prefers-color-scheme: dark` (guarded so an explicit light choice wins)
 * and once under `[data-theme="dark"]` — so switching themes is just
 * setting one attribute on <html>. The same key is read by the pre-paint
 * inline script in each settings-ui/*.html shell, which must run before
 * the stylesheet to avoid a flash of the wrong theme. Keep the key in
 * sync with those shells.
 */
export type Theme = "light" | "dark";

const STORAGE_KEY = "forge-theme";
const listeners = new Set<() => void>();

export function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function currentTheme(): Theme {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === "dark" || explicit === "light") return explicit;
  return systemTheme();
}

export function setTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Private mode / storage disabled: the choice just won't persist
    // across reloads, which is strictly better than throwing here.
  }
  listeners.forEach((listener) => listener());
}

/** For `useSyncExternalStore` — lets components (the theme toggle, the
 * Sonner toaster) re-render on a theme change without polling. */
export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
