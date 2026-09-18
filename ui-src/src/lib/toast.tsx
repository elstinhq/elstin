import { toast } from "sonner";

type Appearance = "success" | "error" | "warning" | "info";

interface ToastContextValue {
  showToast: (heading: string, appearance: Appearance) => void;
}

// A thin, stable wrapper over sonner's toast() — kept as a hook (rather
// than importing `toast` directly at every call site) so call sites read
// `showToast(message, appearance)` regardless of which toast library sits
// underneath.
export function useToast(): ToastContextValue {
  return {
    showToast(heading, appearance) {
      toast[appearance](heading);
    },
  };
}
