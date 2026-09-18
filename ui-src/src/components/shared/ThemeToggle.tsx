import { useSyncExternalStore } from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "../ui/button";
import { currentTheme, setTheme, subscribeTheme, type Theme } from "../../lib/theme";

export function ThemeToggle() {
  // Read through the shared store rather than local state: the pre-paint
  // shell script has already set data-theme by the time this mounts, and
  // subscribing (instead of reading once in an effect) keeps this in sync
  // if the toggle is ever rendered in more than one place at once.
  const theme = useSyncExternalStore<Theme>(subscribeTheme, currentTheme, () => "light");

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? <Sun /> : <Moon />}
    </Button>
  );
}
