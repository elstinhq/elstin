// Bundles the settings UI's TypeScript/React source into static assets
// settings-ui/vendor/ can serve directly, since elstin's UI has no
// other build step and no server-side rendering. Run via `npm run build`
// (see package.json) or from the Dockerfile's ui-build stage.
//
// Every dependency is public (react, react-dom, @tanstack/react-query,
// Radix UI, Tailwind, esbuild, typescript), so this runs anywhere `npm
// install` does — no registry credentials, no private packages. That is a
// deliberate property: the design system this UI used to depend on was
// gated behind a private registry, which meant neither CI nor the Docker
// image nor a fresh clone could build the frontend at all.
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import * as esbuild from "esbuild";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(__dirname, "..", "settings-ui", "vendor");

mkdirSync(outDir, { recursive: true });
// Chunk names carry a content hash, so without this every build would leave
// the previous build's chunks behind.
rmSync(path.join(outDir, "chunks"), { recursive: true, force: true });

// One entry: the app (src/components/App.tsx), which routes on the client.
// settings-ui/app.html loads it with
// `<script type="module" src="/settings-ui/vendor/app.js">`, and FastAPI
// serves that one shell for every page URL (elstin/app.py).
//
// `splitting` makes each dynamic import() its own file under chunks/, loaded
// on demand: today that is mermaid (components/shared/MermaidDiagram.tsx),
// which is far too large to ship to every page that never draws a diagram.
await esbuild.build({
  entryPoints: {
    app: path.join(__dirname, "src", "entries", "app.tsx"),
  },
  outdir: outDir,
  bundle: true,
  format: "esm",
  splitting: true,
  chunkNames: "chunks/[name]-[hash]",
  jsx: "automatic",
  minify: true,
  sourcemap: false,
});

// Compiled rather than imported from the entry: the HTML shell links it with
// a <link>, so it loads in parallel with the bundle instead of delaying first
// paint until the JS parses. The Tailwind CLI (not a bundler plugin) does the compiling —
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
