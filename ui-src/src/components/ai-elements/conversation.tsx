import * as React from "react";
import { ArrowDown } from "lucide-react";
import { Button } from "../ui/button";
import { cn } from "../../lib/utils";

// Vendored in the style of Vercel's AI Elements (Conversation): a scroller
// that follows new content while the reader is at the bottom and stops
// following the moment they scroll up, with a way back down. Written here
// rather than pulled with the AI Elements CLI, which fetches from a registry
// at add-time (see CLAUDE.md on keeping the frontend build offline).
const STICK_THRESHOLD_PX = 24;

export function Conversation({
  className,
  children,
  /** Changes whenever content is added; the scroller follows it while stuck. */
  version,
}: {
  className?: string;
  children: React.ReactNode;
  version: number | string;
}) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = React.useState(true);

  React.useLayoutEffect(() => {
    const el = ref.current;
    if (el && stuck) el.scrollTop = el.scrollHeight;
  }, [version, stuck]);

  const onScroll = () => {
    const el = ref.current;
    if (el) setStuck(el.scrollHeight - el.scrollTop - el.clientHeight <= STICK_THRESHOLD_PX);
  };

  return (
    <div className="relative">
      <div ref={ref} onScroll={onScroll} className={cn("overflow-y-auto", className)} role="log" aria-live="polite">
        {children}
      </div>
      {stuck ? null : (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="absolute bottom-2 left-1/2 -translate-x-1/2 rounded-full shadow-md"
          onClick={() => setStuck(true)}
        >
          <ArrowDown className="size-3.5" /> Latest
        </Button>
      )}
    </div>
  );
}
