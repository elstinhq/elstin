import { ChevronRight } from "lucide-react";

export function Twisty({ isOpen, onToggle }: { isOpen: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      className="text-muted-foreground shrink-0 transition-transform"
      style={{ transform: isOpen ? "rotate(90deg)" : undefined }}
      aria-expanded={isOpen}
      onClick={(e) => {
        e.stopPropagation();
        onToggle();
      }}
    >
      <ChevronRight className="size-4" />
    </button>
  );
}
