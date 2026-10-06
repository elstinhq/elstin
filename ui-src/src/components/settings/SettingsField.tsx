import { useEffect, useRef, useState } from "react";
import type { ConfigField } from "../../types/api";
import { useModelOptions } from "../../hooks/useProjectConfig";
import { ModelCombobox } from "../shared/ModelCombobox";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";

interface SettingsFieldProps {
  field: ConfigField;
  saving: boolean;
  onSave: (key: string, value: string) => void;
  /** Project-scoped settings dialog only — global settings has no concept of an override. */
  override?: { onReset: () => void };
  /** The project whose provider lists the models a model field suggests; none = the global provider. */
  projectId?: number;
}

export function SettingsField({ field, saving, onSave, override, projectId }: SettingsFieldProps) {
  // What the server has. The box follows it, so "Reset to global default" puts
  // the inherited value back in the box instead of leaving the old text there.
  const saved = field.secret ? "" : field.value ?? "";
  const [value, setValue] = useState(saved);
  // The last value sent, so a pick followed by the blur that ends it (the same
  // value, before the refetch has brought it back as `saved`) saves once.
  const sent = useRef<string | null>(null);
  useEffect(() => {
    setValue(saved);
    sent.current = null;
  }, [saved]);

  const overrideHint = override ? (
    field.overridden ? (
      <p className="text-muted-foreground text-xs">
        Project override —{" "}
        <button
          type="button"
          className="text-primary underline underline-offset-2"
          onClick={override.onReset}
        >
          Reset to global default
        </button>
      </p>
    ) : (
      <p className="text-muted-foreground text-xs">Using global default</p>
    )
  ) : null;

  // Leaving a field you did not change saves nothing. In a project that matters:
  // a field shows the global value it inherits, and saving that back would pin
  // the project to today's global value.
  function commit(next: string) {
    if (field.secret && next === "") return; // don't clobber a saved secret with a blank blur
    if (next === saved || next === sent.current) return;
    sent.current = next;
    // A project that blanks a model goes back to inheriting, rather than
    // holding an empty override. (Other settings can mean something blank.)
    if (override && field.type === "model" && field.overridden && next.trim() === "") {
      override.onReset();
      return;
    }
    onSave(field.key, next);
  }

  if (field.type === "bool") {
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-3">
          <Label htmlFor={`field-${field.key}`} className="font-normal leading-snug">
            {field.label}
          </Label>
          <Switch
            id={`field-${field.key}`}
            checked={String(field.value).toLowerCase() === "true"}
            disabled={saving}
            onCheckedChange={(checked) => onSave(field.key, checked ? "true" : "false")}
          />
        </div>
        {overrideHint}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`field-${field.key}`} className="leading-snug">
          {field.label}
        </Label>
        <div className="flex items-center gap-2">
          {field.type === "model" ? (
            <ModelField field={field} projectId={projectId} value={value} onChange={setValue} onCommit={commit} />
          ) : (
            <Input
              id={`field-${field.key}`}
              type={field.secret ? "password" : "text"}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onBlur={() => commit(value)}
            />
          )}
          {saving ? <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent" aria-label="Saving" /> : null}
        </div>
        {field.secret && field.is_set && (!override || field.overridden) ? (
          <p className="text-muted-foreground text-xs">Set — leave blank to keep the current value.</p>
        ) : null}
      </div>
      {overrideHint}
    </div>
  );
}

// A reason from the server, as a sentence of its own.
function sentence(text: string): string {
  const trimmed = text.trim();
  const capitalized = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capitalized) ? capitalized : `${capitalized}.`;
}

// A model field: the provider's models as suggestions, any id typeable.
function ModelField({
  field,
  projectId,
  value,
  onChange,
  onCommit,
}: {
  field: ConfigField;
  projectId?: number;
  value: string;
  onChange: (value: string) => void;
  onCommit: (value: string) => void;
}) {
  const options = useModelOptions(projectId);
  const models = options.data?.models ?? [];
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <ModelCombobox id={`field-${field.key}`} value={value} options={models} onChange={onChange} onCommit={onCommit} />
      {options.data ? (
        <p className="text-muted-foreground text-xs">
          {options.data.source === "provider"
            ? `${models.length} models from your provider. Any model id can be typed.`
            : `${sentence(options.data.error ?? "The provider's list is unavailable")} Suggesting the models Elstin has prices for. Any model id can be typed.`}
        </p>
      ) : null}
    </div>
  );
}
