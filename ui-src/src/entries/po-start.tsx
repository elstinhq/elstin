import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "../lib/queryClient";
import { Toaster } from "../components/ui/sonner";
import { PoStartPage } from "../components/project-detail/po/PoStartPage";

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

// /projects/<id>/po/new
const projectId = Number(window.location.pathname.match(/^\/projects\/(\d+)\//)?.[1]);

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PoStartPage projectId={projectId} />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
