import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "../lib/queryClient";
import { Toaster } from "../components/ui/sonner";
import { ProjectDetailPage } from "../components/project-detail/ProjectDetailPage";

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

const projectId = Number(window.location.pathname.split("/").filter(Boolean).pop());

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ProjectDetailPage projectId={projectId} />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
