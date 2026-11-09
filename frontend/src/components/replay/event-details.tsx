import type { Event } from "@/contracts/generated";
import { eventLabel } from "./model";

export function EventDetails({ event }: { event: Event | null }) {
  return <aside aria-label="Event details" className="min-w-0 space-y-3 rounded-xl border bg-card p-4">
    <h2 className="font-semibold">Event details</h2>
    {event ? <>
      <p className="break-words text-sm font-medium">{eventLabel(event)}</p>
      <p className="text-xs text-muted-foreground">#{event.seq} · {event.ts} · {event.actor.name}</p>
      {event.type === "board.entry_posted" && <div className="space-y-2"><h3 className="font-medium">{event.payload.entry.title}</h3><p className="whitespace-pre-wrap break-words text-sm">{event.payload.entry.body}</p><p className="text-xs text-muted-foreground">To: {event.payload.entry.to?.join(", ") || "Everyone"}</p></div>}
      {event.type === "agent.step" && <><h3 className="text-sm font-medium">Reasoning</h3><p className="whitespace-pre-wrap break-words text-sm">{event.payload.reasoning_excerpt || "No reasoning recorded."}</p><h3 className="text-sm font-medium">Tool calls</h3><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify([...event.payload.coordination_calls, ...(event.payload.action ? [event.payload.action] : [])], null, 2)}</pre></>}
      {(event.type === "approval.decided" || event.type === "approval.requested") && <dl className="space-y-2 text-sm"><dt>Action</dt><dd className="whitespace-pre-wrap break-words">{event.payload.approval.action_summary}</dd><dt>Decision</dt><dd>{event.payload.approval.status}</dd><dt>Note</dt><dd className="whitespace-pre-wrap break-words">{event.payload.approval.note || "No note"}</dd></dl>}
      <details><summary className="cursor-pointer text-sm">Full event payload</summary><pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(event.payload, null, 2)}</pre></details>
    </> : <p className="text-sm text-muted-foreground">Select a marker or step to inspect its details.</p>}
  </aside>;
}
