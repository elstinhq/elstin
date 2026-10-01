import { ChevronRight } from "lucide-react";
import type { ConfigField } from "../../types/api";
import type { SettingsSectionData } from "../../lib/settingsSections";
import { Badge } from "../ui/badge";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../ui/collapsible";
import { SettingsField } from "./SettingsField";

interface SettingsSectionProps {
  section: SettingsSectionData;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  savingKey: string | null;
  onSave: (key: string, value: string) => void;
  /** Project settings only: how a field goes back to inheriting the global value. */
  overrideFor?: (field: ConfigField) => { onReset: () => void };
  /** Project settings only: the project whose provider lists the models a model field suggests. */
  projectId?: number;
}

// One collapsible group of settings. The whole header row is the button, so
// the target is large; the heading wraps it so the page still has an outline
// for a screen reader to jump through.
export function SettingsSection({ section, open, onOpenChange, savingKey, onSave, overrideFor, projectId }: SettingsSectionProps) {
  const count = section.fields.length;
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} asChild>
      <section id={section.id} className="bg-card scroll-mt-20 rounded-xl border shadow-xs">
        <h2>
          <CollapsibleTrigger className="group hover:bg-accent/50 focus-visible:ring-ring/50 flex w-full items-center gap-3 rounded-xl px-4 py-3.5 text-left outline-none focus-visible:ring-[3px]">
            <ChevronRight
              className="text-muted-foreground size-4 shrink-0 transition-transform group-data-[state=open]:rotate-90"
              aria-hidden="true"
            />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold">{section.title}</span>
              <span className="text-muted-foreground block text-xs font-normal">{section.description}</span>
            </span>
            <Badge variant="secondary" className="font-normal tabular-nums">
              <span className="sm:hidden">{count}</span>
              <span className="hidden sm:inline">
                {count} {count === 1 ? "setting" : "settings"}
              </span>
            </Badge>
          </CollapsibleTrigger>
        </h2>
        <CollapsibleContent className="data-[state=closed]:animate-collapsible-up data-[state=open]:animate-collapsible-down overflow-hidden motion-reduce:animate-none">
          <div className="border-t px-4 sm:pl-11">
            <div className="divide-border divide-y">
              {section.fields.map((field) => (
                <div key={field.key} className="max-w-xl py-3.5">
                  <SettingsField
                    field={field}
                    saving={savingKey === field.key}
                    onSave={onSave}
                    override={overrideFor?.(field)}
                    projectId={projectId}
                  />
                </div>
              ))}
            </div>
          </div>
        </CollapsibleContent>
      </section>
    </Collapsible>
  );
}
