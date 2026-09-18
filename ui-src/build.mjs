// Bundles the settings UI's TypeScript/React source into static assets
// settings-ui/vendor/ can serve directly, since code_forge's UI has no
// other build step and no server-side rendering. Run via `npm run build`
// (see package.json) or from the Dockerfile's ui-build stage.
//
// Every dependency is public (react, react-dom, @tanstack/react-query,
// Radix UI, Tailwind, esbuild, typescript), so this runs anywhere `npm
// install` does — no registry credentials, no private packages. That is a
// deliberate property: the design system this UI used to depend on was
// gated behind a private registry, which meant neither CI nor the Docker
// image nor a fresh clone could build the frontend at all.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import * as esbuild from "esbuild";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(__dirname, "..", "settings-ui", "vendor");

mkdirSync(outDir, { recursive: true });

// One entry per settings-ui/*.html page — see settings-ui/*.html's
// `<script type="module" src="/settings-ui/vendor/app-*.js">` tags. Each
// mounts its own React root; there is no client-side router (see AGENTS.md
// on why this stayed multi-page).
await esbuild.build({
  entryPoints: {
    "app-projects": path.join(__dirname, "src", "entries", "projects.tsx"),
    "app-settings": path.join(__dirname, "src", "entries", "settings.tsx"),
    "app-project-detail": path.join(__dirname, "src", "entries", "project-detail.tsx"),
  },
  outdir: outDir,
  bundle: true,
  format: "esm",
  jsx: "automatic",
  minify: true,
  sourcemap: false,
});

// Compiled rather than imported from an entry: each HTML shell links it
// once with a <link>, so bundling it into all three JS entries would ship
// three copies of the same stylesheet and delay first paint until JS
// parses. The Tailwind CLI (not a bundler plugin) does the compiling —
// esbuild has no CSS-transform story of its own, and this is the one
// piece of the build shadcn/ui actually needs Tailwind for.
const tailwind = spawnSync(
  process.execPath,
  [
    path.join(__dirname, "node_modules", "@tailwindcss", "cli", "dist", "index.mjs"),
    "-i",
    path.join(__dirname, "src", "styles", "globals.css"),
    "-o",
    path.join(outDir, "app.css"),
    "--minify",
  ],
  { cwd: __dirname, stdio: "inherit" },
);
if (tailwind.status !== 0) {
  throw new Error("Tailwind CSS build failed");
}

console.log("UI bundle built ->", outDir);
