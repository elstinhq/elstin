import type { CSSProperties } from "react";
import { useSyncExternalStore } from "react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

import { subscribeTheme, currentTheme } from "../../lib/theme";

function Toaster(props: ToasterProps) {
  const theme = useSyncExternalStore(subscribeTheme, currentTheme, () => "light" as const);

  return (
    <Sonner
      theme={theme}
      className="toaster group"
      style={
        {
          "--normal-bg": "var(--popover)",
          "--normal-text": "var(--popover-foreground)",
          "--normal-border": "var(--border)",
        } as CSSProperties
      }
      {...props}
    />
  );
}

export { Toaster };
