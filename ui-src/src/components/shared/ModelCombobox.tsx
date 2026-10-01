import { useEffect, useId, useMemo, useRef, useState } from "react";
import { cn } from "../../lib/utils";
import { Input } from "../ui/input";

// A provider can list several hundred models (OpenRouter does). Rendering all
// of them makes the list heavy and unreadable; the rest is one keystroke away.
const MAX_SHOWN = 200;

interface ModelComboboxProps {
  id: string;
  value: string;
  options: string[];
  disabled?: boolean;
  /** Every keystroke and every pick. */
  onChange: (value: string) => void;
  /** A pick, Enter, or leaving the field: the value is final. */
  onCommit: (value: string) => void;
}

// A text field with a list of suggestions under it. Not a <select>: the
// suggestions are only that, and a model id forge has never heard of must be
// typeable. Not a native datalist either — that narrows its suggestions to the
// text already in the box, so a field holding the current model would offer
// nothing else. Here the whole list shows until the person starts typing.
//
// The list sits in the page flow rather than floating over it, so a dialog's
// scroll container cannot clip it.
export function ModelCombobox({ id, value, options, disabled, onChange, onCommit }: ModelComboboxProps) {
  const listId = useId();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState(false); // edited since the list opened: only then does it narrow
  const [active, setActive] = useState(-1);
  const listRef = useRef<HTMLUListElement>(null);

  const trimmed = value.trim();
  const needle = typed ? trimmed.toLowerCase() : "";
  const matches = useMemo(
    () => (needle ? options.filter((option) => option.toLowerCase().includes(needle)) : options),
    [options, needle],
  );
  const shown = useMemo(() => matches.slice(0, MAX_SHOWN), [matches]);
  const isCustom = typed && trimmed !== "" && !options.includes(trimmed);

  // Opening the list lands on the current model, so it is in view and one
  // arrow key from its neighbours.
  useEffect(() => {
    if (!open || typed) return;
    setActive(shown.indexOf(trimmed));
  }, [open, typed, trimmed, shown]);

  useEffect(() => {
    if (active < 0) return;
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function close() {
    setOpen(false);
    setTyped(false);
    setActive(-1);
  }

  function pick(option: string) {
    onChange(option);
    onCommit(option);
    close();
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) setOpen(true);
      else setActive((index) => Math.min(index + 1, shown.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => Math.max(index - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault(); // never submit the form around it
      if (open && active >= 0 && shown[active] !== undefined) pick(shown[active]);
      else {
        onCommit(value);
        close();
      }
    } else if (event.key === "Escape" && open) {
      close();
    }
  }

  return (
    <div className="flex flex-col">
      <Input
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
          setTyped(true);
          setOpen(true);
          setActive(-1); // the list just changed under the highlight; the first ArrowDown starts from the top
        }}
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        onKeyDown={onKeyDown}
        onBlur={() => {
          // Options take the pick on mousedown without moving focus, so a blur
          // is always the person leaving the field.
          onCommit(value);
          close();
        }}
      />
      {open ? (
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label="Suggested models"
          className="bg-popover text-popover-foreground mt-1 max-h-56 overflow-auto rounded-md border text-sm shadow-xs"
        >
          {shown.map((option, index) => (
            <li
              key={option}
              id={`${listId}-${index}`}
              data-index={index}
              role="option"
              aria-selected={option === trimmed}
              className={cn(
                "cursor-pointer px-3 py-1.5 break-all",
                index === active && "bg-accent text-accent-foreground",
                option === trimmed && "font-medium",
              )}
              onMouseDown={(event) => {
                event.preventDefault();
                pick(option);
              }}
              onMouseEnter={() => setActive(index)}
            >
              {option}
            </li>
          ))}
          {shown.length === 0 ? (
            <li role="presentation" className="text-muted-foreground px-3 py-1.5 text-xs">
              {options.length === 0 ? "No suggestions — type a model id." : "No suggested model matches."}
            </li>
          ) : null}
          {matches.length > MAX_SHOWN ? (
            <li role="presentation" className="text-muted-foreground border-t px-3 py-1.5 text-xs">
              Showing {MAX_SHOWN} of {matches.length} — keep typing to narrow.
            </li>
          ) : null}
          {isCustom ? (
            <li role="presentation" className="text-muted-foreground border-t px-3 py-1.5 text-xs">
              Press Enter to use “{trimmed}”.
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}
