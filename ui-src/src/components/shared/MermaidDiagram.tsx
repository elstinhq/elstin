import { useEffect, useId, useState, useSyncExternalStore } from "react";
import { currentTheme, subscribeTheme } from "../../lib/theme";

// Mermaid is several MB, so it is imported on first use rather than at the
// top of the file: build.mjs turns this dynamic import into its own chunk, and
// a page that never shows a diagram never downloads it.
type Mermaid = typeof import("mermaid").default;
let mermaidPromise: Promise<Mermaid> | null = null;

function loadMermaid(): Promise<Mermaid> {
  mermaidPromise ??= import("mermaid").then((m) => m.default);
  return mermaidPromise;
}

type Rendered = { svg: string } | { error: string };

/** A fenced ```mermaid block drawn as a diagram. Falls back to the source
 * text when mermaid rejects it — planner output is model-written, so a
 * diagram that doesn't parse is an ordinary case, not an exception. */
export function MermaidDiagram({ code }: { code: string }) {
  // useId yields ":r1:"-style ids, which mermaid uses in a CSS selector.
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const theme = useSyncExternalStore(subscribeTheme, currentTheme);
  const [rendered, setRendered] = useState<Rendered | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRendered(null);
    (async () => {
      try {
        const mermaid = await loadMermaid();
        // securityLevel "strict" (the default, stated here because the diagram
        // text is model output and the result is injected as HTML) sanitizes
        // labels and disables click handlers.
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          suppressErrorRendering: true,
          theme: theme === "dark" ? "dark" : "default",
        });
        const { svg } = await mermaid.render(id, code);
        if (!cancelled) setRendered({ svg });
      } catch (err) {
        // A failed render can leave its scratch node behind in <body>.
        document.getElementById(`d${id}`)?.remove();
        if (!cancelled) setRendered({ error: err instanceof Error ? err.message : String(err) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, id, theme]);

  if (rendered && "error" in rendered) {
    return (
      <div className="flex min-w-0 flex-col gap-1">
        <span className="text-muted-foreground text-xs">Couldn't render this diagram — showing its source.</span>
        <pre className="font-mono text-xs whitespace-pre-wrap">{code}</pre>
      </div>
    );
  }
  if (!rendered) {
    return <div className="text-muted-foreground py-6 text-center text-xs">Rendering diagram…</div>;
  }
  return (
    <div
      role="img"
      aria-label="Diagram"
      className="border-border bg-background min-w-0 overflow-x-auto rounded-md border p-3 [&_svg]:mx-auto [&_svg]:h-auto"
      dangerouslySetInnerHTML={{ __html: rendered.svg }}
    />
  );
}
