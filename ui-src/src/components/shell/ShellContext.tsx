import { createContext, useContext } from "react";

// Lets a page's top bar open the sidebar, which on a narrow screen lives in a
// sheet the layout owns.
export const ShellContext = createContext<{ openNav: () => void }>({ openNav: () => {} });

export function useShell() {
  return useContext(ShellContext);
}
