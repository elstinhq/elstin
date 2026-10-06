import { useEffect, useRef, useState } from "react";
import { MessageSquare } from "lucide-react";
import { useSendPoMessage } from "../../../hooks/usePoSessions";
import { useToast } from "../../../lib/toast";
import { cn } from "../../../lib/utils";
import type { PoSession } from "../../../types/api";
import { Button } from "../../ui/button";
import { Textarea } from "../../ui/textarea";

const MAX_CHARS = 4000;
const CLOSED = new Set(["discarded", "publishing", "published"]);

// The conversation around the draft, docked at the foot of the page's right-hand rail
// so it stays in reach while the draft scrolls. A message sent while the agent is
// working reaches it between two of its steps; sent while it is not, it queues a
// revision of the draft — told which fields a person has edited so it does not undo
// them. It fills the height the rail gives it: messages scroll, the box stays put.
export function PoChat({ projectId, session }: { projectId: number; session: PoSession }) {
  const [message, setMessage] = useState("");
  const send = useSendPoMessage(projectId, session.id);
  const { showToast } = useToast();
  const messages = session.messages ?? [];
  const box = useRef<HTMLDivElement | null>(null);
  const closed = CLOSED.has(session.status);
  const working = session.status === "queued" || session.status === "running";
  const text = message.trim();

  useEffect(() => {
    // The box itself, not scrollIntoView: that would scroll the whole page too.
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [messages.length, working]);

  function submit() {
    if (!text || closed) return;
    send.mutate(text, {
      onSuccess: (result) => {
        setMessage("");
        showToast(
          result.delivery === "live" ? "Sent — the agent reads it before its next step" : "Sent — the agent will revise the draft",
          "success",
        );
      },
      onError: (err) => showToast(err.message || "Could not send this message", "error"),
    });
  }

  return (
    <section
      aria-label="Conversation with the PO agent"
      className="bg-card flex min-h-0 flex-col overflow-hidden rounded-xl border max-lg:h-[30rem] lg:flex-1"
    >
      <header className="flex items-center gap-2 border-b px-3 py-2.5">
        <MessageSquare className="text-muted-foreground size-4" aria-hidden="true" />
        <h2 className="text-sm font-medium">Conversation</h2>
        {session.edited_by_person ? <span className="text-muted-foreground ml-auto text-xs">Your edits are kept</span> : null}
      </header>

      <div ref={box} className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-3 py-3" aria-live="polite">
        {messages.length === 0 && !working ? (
          <p className="text-muted-foreground m-auto max-w-[16rem] text-center text-sm">
            Ask the agent to change the draft, or answer what it asks.
          </p>
        ) : null}
        {messages.map((m) => (
          <div
            key={m.id}
            className={cn(
              "max-w-[88%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap",
              m.role === "person" ? "bg-primary text-primary-foreground self-end rounded-br-md" : "bg-muted self-start rounded-bl-md",
            )}
          >
            {m.text || <span className="text-muted-foreground italic">(no reply)</span>}
            {m.role === "person" && !m.delivered && session.status !== "queued" ? (
              <span className="mt-1 block text-[11px] opacity-70">waiting for the agent to read it</span>
            ) : null}
          </div>
        ))}
        {working ? (
          <div className="bg-muted text-muted-foreground flex items-center gap-2 self-start rounded-2xl rounded-bl-md px-3 py-2 text-sm">
            <span className="bg-blue-500 size-1.5 animate-pulse rounded-full motion-reduce:animate-none" aria-hidden="true" />
            {session.status === "queued" ? "Waiting for the daemon to pick this up (up to 15s)…" : "The agent is working…"}
          </div>
        ) : null}
      </div>

      <div className="flex flex-col gap-2 border-t p-2.5">
        <Textarea
          aria-label="Message to the PO agent"
          className="max-h-32 min-h-14 resize-none"
          maxLength={MAX_CHARS}
          disabled={closed}
          placeholder={
            closed
              ? "This session is closed."
              : session.draft
                ? "Ask for a change — e.g. split s2 in two, or add a story for notifications"
                : "Add to the brief, or ask a question"
          }
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit();
          }}
        />
        <div className="flex items-center justify-between gap-3">
          <span className="text-muted-foreground text-xs">⌘/Ctrl+Enter sends</span>
          <Button size="sm" disabled={!text || closed} loading={send.isPending} onClick={submit}>
            Send
          </Button>
        </div>
      </div>
    </section>
  );
}
