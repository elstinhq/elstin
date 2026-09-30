import { useState } from "react";
import type { ConfigField } from "../../types/api";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";

interface SettingsFieldProps {
  field: ConfigField;
  saving: boolean;
  onSave: (key: string, value: string) => void;
  /** Project-scoped settings dialog only — global settings has no concept of an override. */
  override?: { onReset: () => void };
}

export function SettingsField({ field, saving, onSave, override }: SettingsFieldProps) {
  const [value, setValue] = useState(field.secret ? "" : field.value ?? "");

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
          <Input
            id={`field-${field.key}`}
            type={field.secret ? "password" : "text"}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => {
              if (field.secret && value === "") return; // don't clobber a saved secret with a blank blur
              onSave(field.key, value);
            }}
          />
          {saving ? <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent" aria-label="Saving" /> : null}
        </div>
        {field.secret ? (
          <p className="text-muted-foreground text-xs">
            {field.is_set ? "Set — leave blank to keep the current value." : ""}
          </p>
        ) : null}
      </div>
      {overrideHint}
    </div>
  );
}
