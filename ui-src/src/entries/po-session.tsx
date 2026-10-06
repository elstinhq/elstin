import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "../lib/queryClient";
import { Toaster } from "../components/ui/sonner";
import { PoSessionPage } from "../components/project-detail/po/PoSessionPage";

const root = document.getElementById("root");
if (!root) throw new Error("#root not found");

// /projects/<id>/po/<session>
const [, projectId, sessionId] = window.location.pathname.match(/^\/projects\/(\d+)\/po\/(\d+)/) ?? [];

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PoSessionPage projectId={Number(projectId)} sessionId={Number(sessionId)} />
      <Toaster />
    </QueryClientProvider>
  </StrictMode>,
);
