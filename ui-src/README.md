# elstin UI build

`src/` is a TypeScript + React app with one entry point
(`src/entries/app.tsx`) mounted into the one shell, `../settings-ui/app.html`,
which FastAPI serves for every page URL (`../elstin/app.py`). The app routes
on the client: `src/components/App.tsx` is the route table and
`src/lib/router.tsx` the router (history API, no dependency).

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
sonner, `@yogesharc/thinking-orbs` (the PO agent's working orb, MIT, no
dependencies of its own), `mermaid` (draws the flow charts in a plan; it is
several MB, so `MermaidDiagram` imports it on first use and `build.mjs`
splits it into `vendor/chunks/` rather than into `app.js`), esbuild,
typescript — which is what lets CI, a fresh clone, and
the Docker image all build the frontend. Keep it that way: a dependency
that needs registry credentials, or a build step that fetches from one
(like the shadcn CLI), takes the whole frontend build offline again.

Theming is one attribute, not shadcn's default `.dark` class. `<html>`
uses `data-theme="dark"|"light"` (set by `lib/theme.ts`, persisted under
`elstin-theme`) because that is what the pre-paint inline script in
`../settings-ui/app.html` sets from `localStorage` before first paint — `globals.css`'s `@custom-variant dark` selector points at
that attribute instead of the class shadcn scaffolds by default. Keep the
storage key in sync between `lib/theme.ts` and that shell.

## Build

```bash
cd ui-src
npm install
npm run build      # -> ../settings-ui/vendor/app.js + app.css
npm run typecheck  # tsc --noEmit — not wired into any CI, run it by hand
```

No registry configuration, no `~/.npmrc`, no credentials.

`npm run build` bundles the entry point with esbuild (built-in TSX/JSX
support, no extra plugin), then runs the Tailwind CLI to compile
`src/styles/globals.css` into `../settings-ui/vendor/app.css`. The
stylesheet is compiled standalone rather than imported from the entry so
the shell can `<link>` it and paint before the JS parses.

Rebuild after any change under `src/`. `../settings-ui/vendor/` is
gitignored; `../Dockerfile`'s `ui-build` stage runs this same build, so
`docker compose up --build` needs no prior local build.
