# code_forge UI build

`src/` is a TypeScript + React app — three independent entry points
(`src/entries/projects.tsx`, `settings.tsx`, `project-detail.tsx`), one per
page under `../settings-ui/*.html`. There is no client-side router: each
entry mounts its own React root into that page's `<div id="root">`,
matching the three routes FastAPI already serves
(`../forge/app.py`) — navigation between pages is still a plain page load.

## Components

UI primitives are shadcn/ui: `src/components/ui/` holds Radix UI
components (dialog, select, switch, tooltip, etc.) styled with Tailwind
utility classes, vendored as source rather than pulled in with the shadcn
CLI — the CLI fetches component source from an external registry at
add-time, which would take the frontend build offline again for the same
reason the CLI-less setup below matters. Page-level components under
`src/components/{projects,project-detail,settings,shared}/` compose those
primitives; only `components/ui/*` imports a `@radix-ui/*` package
directly. Icons are `lucide-react`; toasts are `sonner`, wrapped by
`lib/toast.tsx` so call sites read `showToast(message, appearance)`
regardless of which toast library sits underneath.

Design tokens (colors, radius) live as CSS variables in
`src/styles/globals.css`, following shadcn's default palette — light and
dark blocks, referenced by Tailwind's `@theme inline`. That file is
compiled by the Tailwind CLI (not imported from a TS entry) into
`../settings-ui/vendor/app.css`, then linked once from each HTML shell —
see Build below.

Props are ordinary React: `onClick`, `onChange={(e) =>
...e.target.value}`, `onOpenChange`. Radix's `asChild`/`Slot` pattern
(e.g. `<Button asChild><a href=...>`) needs its child to be a single
React element — `Button`'s loading spinner is skipped when `asChild` is
set, since adding a sibling node would break `Slot`.

This replaced a private, registry-gated design system, then a hand-rolled
one (`src/ds/`) built to get off it. **Every dependency is still public**
— react, react-dom, TanStack Query, Radix UI, Tailwind, lucide-react,
sonner, esbuild, typescript — which is what lets CI, a fresh clone, and
the Docker image all build the frontend. Keep it that way: a dependency
that needs registry credentials, or a build step that fetches from one
(like the shadcn CLI), takes the whole frontend build offline again.

Theming is one attribute, not shadcn's default `.dark` class. `<html>`
uses `data-theme="dark"|"light"` (set by `lib/theme.ts`, persisted under
`forge-theme`) because that is what the pre-paint inline script in each
`../settings-ui/*.html` shell already sets from `localStorage` before
first paint — `globals.css`'s `@custom-variant dark` selector points at
that attribute instead of the class shadcn scaffolds by default. Keep the
storage key in sync between `lib/theme.ts` and those shells.

## Build

```bash
cd ui-src
npm install
npm run build      # -> ../settings-ui/vendor/app-{projects,settings,project-detail}.js + app.css
npm run typecheck  # tsc --noEmit — not wired into any CI, run it by hand
```

No registry configuration, no `~/.npmrc`, no credentials.

`npm run build` bundles all three entry points with esbuild (built-in
TSX/JSX support, no extra plugin), then runs the Tailwind CLI to compile
`src/styles/globals.css` into `../settings-ui/vendor/app.css`. The
stylesheet is compiled standalone rather than imported from an entry so
the HTML shells can `<link>` it once, instead of shipping three bundled
copies and deferring first paint until JS parses.

Rebuild after any change under `src/`. `../settings-ui/vendor/` is
gitignored; `../Dockerfile`'s `ui-build` stage runs this same build, so
`docker compose up --build` needs no prior local build.
