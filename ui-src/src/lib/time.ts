const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();

/** A list's "Updated" cell: "Just now", "12m ago", "2h ago", "Yesterday",
 * "3d ago", then the date ("Sep 28", with the year once it is not this one's).
 * Empty for a timestamp that does not parse. */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const elapsed = now.getTime() - then.getTime();
  // Also a timestamp a little ahead of this browser's clock.
  if (elapsed < MINUTE) return "Just now";
  if (elapsed < HOUR) return `${Math.floor(elapsed / MINUTE)}m ago`;
  if (elapsed < DAY) return `${Math.floor(elapsed / HOUR)}h ago`;
  // Whole calendar days, not 24h blocks: 26 hours ago can still be two days back.
  const days = Math.round((startOfDay(now) - startOfDay(then)) / DAY);
  if (days <= 1) return "Yesterday";
  if (days < 7) return `${days}d ago`;
  return then.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: then.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}
