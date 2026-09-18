import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "../lib/queryClient";
import { Toaster } from "../components/ui/sonner";
import { ProjectsPage } from "../components/projects/ProjectsPage";

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ProjectsPage />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
