import { Plus, X } from "lucide-react";
import { Button } from "../../ui/button";
import { Textarea } from "../../ui/textarea";

// An editable list of short texts — acceptance criteria, gaps, assumptions,
// what is out of scope. Each item is a bordered row you type into, with its
// remove cross inside it; a blank one is what "add" leaves for typing into, and
// is dropped on save.
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
      <span className="text-muted-foreground text-xs">{label}</span>
      {items.length === 0 && emptyHint ? <p className="text-muted-foreground text-xs">{emptyHint}</p> : null}
      {items.map((item, index) => (
        <div
          className="border-input focus-within:border-ring focus-within:ring-ring/50 flex items-start gap-2 rounded-md border py-1.5 pr-2 pl-2.5 transition-[color,box-shadow] focus-within:ring-[3px]"
          key={index}
        >
          <Textarea
            className="min-h-0 flex-1 resize-none border-0 bg-transparent p-0 shadow-none focus-visible:ring-0"
            rows={1}
            aria-label={`${label} ${index + 1}`}
            value={item}
            disabled={readOnly}
            onChange={(e) => onChange(items.map((existing, i) => (i === index ? e.target.value : existing)))}
          />
          {!readOnly ? (
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground mt-0.5 shrink-0 rounded p-0.5"
              aria-label={`Remove ${label.toLowerCase()} ${index + 1}`}
              onClick={() => onChange(items.filter((_, i) => i !== index))}
            >
              <X className="size-3.5" />
            </button>
          ) : null}
        </div>
      ))}
      {!readOnly ? (
        <Button variant="ghost" className="text-muted-foreground h-6 w-fit gap-1 px-1 text-xs [&_svg:not([class*='size-'])]:size-3" onClick={() => onChange([...items, ""])}>
          <Plus />
          {addLabel}
        </Button>
      ) : null}
    </div>
  );
}
