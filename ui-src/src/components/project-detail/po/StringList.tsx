import { Plus, X } from "lucide-react";
import { Button } from "../../ui/button";
import { Textarea } from "../../ui/textarea";

// An editable list of short texts — acceptance criteria, gaps, assumptions,
// what is out of scope. Each item is its own box; a blank one is what "add"
// leaves for typing into, and is dropped on save.
export function StringList({
  label,
  items,
  onChange,
  readOnly,
  addLabel = "Add",
  emptyHint,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  readOnly: boolean;
  addLabel?: string;
  emptyHint?: string;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      {items.length === 0 && emptyHint ? <p className="text-muted-foreground text-xs">{emptyHint}</p> : null}
      {items.map((item, index) => (
        <div className="flex items-start gap-1.5" key={index}>
          <Textarea
            className="min-h-9 py-1.5"
            aria-label={`${label} ${index + 1}`}
            value={item}
            disabled={readOnly}
            onChange={(e) => onChange(items.map((existing, i) => (i === index ? e.target.value : existing)))}
          />
          {!readOnly ? (
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              aria-label={`Remove ${label.toLowerCase()} ${index + 1}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
            >
              <X />
            </Button>
          ) : null}
        </div>
      ))}
      {!readOnly ? (
        <Button variant="ghost" size="sm" className="w-fit" onClick={() => onChange([...items, ""])}>
          <Plus />
          {addLabel}
        </Button>
      ) : null}
    </div>
  );
}
