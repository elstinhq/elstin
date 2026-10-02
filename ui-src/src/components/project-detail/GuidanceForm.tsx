import { useState } from "react";
import { useSendGuidance } from "../../hooks/useStories";
import { useToast } from "../../lib/toast";
import { Button } from "../ui/button";
import { Textarea } from "../ui/textarea";

const MAX_CHARS = 4000;

// A message to the agent while it works. Cancel used to be the only control on a
// running story: a person who watched dev head the wrong way could stop it, not
// say "use the helper that already exists". The agent reads the message at the
// start of its next turn; it is advice, not authority — dev still writes only the
// files the approved plan names.
export function GuidanceForm({ storyKey }: { storyKey: string }) {
  const [message, setMessage] = useState("");
  const sendGuidance = useSendGuidance();
  const { showToast } = useToast();
  const text = message.trim();

  function handleSend() {
    if (!text) return;
    sendGuidance.mutate(
      { storyKey, message: text },
      {
        onSuccess: () => {
          setMessage("");
          showToast("Sent — the agent reads it at the start of its next step", "success");
        },
        onError: (err) => showToast(err.message || "Could not send this message", "error"),
      },
    );
  }

  return (
    <div className="mt-3 rounded-md border p-3">
      <div className="mb-2 text-sm font-medium">Message the agent</div>
      <Textarea
        value={message}
        maxLength={MAX_CHARS}
        placeholder="e.g. Use the existing formatDate helper in utils/dates.ts instead of writing a new one."
        aria-label="Message to the agent"
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          if ((e.metaKey || e.ctrlKey) && e.key === "Enter") handleSend();
        }}
      />
      <div className="mt-2 flex items-center justify-between gap-3">
        <span className="text-muted-foreground text-xs">
          Read at the start of its next step. It can still change only the files in the approved plan.
        </span>
        <Button size="sm" className="shrink-0" disabled={!text} loading={sendGuidance.isPending} onClick={handleSend}>
          Send
        </Button>
      </div>
    </div>
  );
}
