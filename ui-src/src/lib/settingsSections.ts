import type { ConfigCategory, ConfigField } from "../types/api";

export interface SettingsSectionData {
  id: string;
  title: string;
  description: string;
  fields: ConfigField[];
}

// The server owns which section a field is in and the order inside it
// (forge/config/store.py's CONFIG_CATEGORIES). A saved value with no section of
// its own still has to be reachable, so it lands in a last one. Sections with
// nothing in them — a project only overrides some of the settings — are left
// out.
export function buildSections(fields: ConfigField[], categories: ConfigCategory[] | undefined): SettingsSectionData[] {
  const byKey = new Map(fields.map((field) => [field.key, field]));
  const placed = new Set<string>();
  const sections: SettingsSectionData[] = [];
  for (const category of categories ?? []) {
    const inSection = category.keys.map((key) => byKey.get(key)).filter((field): field is ConfigField => field !== undefined);
    for (const field of inSection) placed.add(field.key);
    if (inSection.length > 0) {
      sections.push({ id: category.id, title: category.title, description: category.description, fields: inSection });
    }
  }
  const rest = fields.filter((field) => !placed.has(field.key));
  if (rest.length > 0) {
    sections.push(
      categories
        ? { id: "other", title: "Other", description: "Saved values that have no section of their own.", fields: rest }
        : { id: "all", title: "All settings", description: "", fields: rest },
    );
  }
  return sections;
}
