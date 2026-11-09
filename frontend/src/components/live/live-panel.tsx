"use client";

import { Tabs } from "radix-ui";
import type { Event } from "@/contracts/generated";
import type { SessionState } from "@/lib/session-store";
import { ApprovalsPanel } from "./approvals-panel";
import { BoardPanel } from "./board-panel";
import { TasksPanel } from "./tasks-panel";

export function LivePanel({ sessionId, state }: { sessionId: string; state: SessionState }) {
  const agents = Object.values(state.agents);
  const pending = Object.values(state.approvals).filter(approval => approval.status === "pending");
  return <Tabs.Root defaultValue="board" className="p-3">
    <Tabs.List aria-label="Session panel tabs" className="sticky top-0 z-10 flex flex-wrap gap-1 border-b bg-card pb-3">
      {["Board", "Tasks", "Supervisor", "Approvals"].map(label => <Tabs.Trigger key={label} value={label.toLowerCase()} className="rounded-md px-2 py-2 text-xs font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring data-[state=active]:bg-muted">
        {label}{label === "Approvals" && <span className="ml-1 rounded-full bg-amber-500/20 px-1.5" aria-label={`${pending.length} pending approvals`}>{pending.length}</span>}
      </Tabs.Trigger>)}
    </Tabs.List>
    <Tabs.Content value="board" className="pt-4"><BoardPanel sessionId={sessionId} entries={state.board} agents={agents} /></Tabs.Content>
    <Tabs.Content value="tasks" className="pt-4"><TasksPanel tasks={Object.values(state.tasks)} artifacts={state.artifacts} /></Tabs.Content>
    <Tabs.Content value="supervisor" className="pt-4"><SupervisorFeed events={state.supervisorFeed} /></Tabs.Content>
    <Tabs.Content value="approvals" className="pt-4"><ApprovalsPanel approvals={pending} agents={agents} /></Tabs.Content>
  </Tabs.Root>;
}

function SupervisorFeed({ events }: { events: Event[] }) {
  return events.length ? <ol className="space-y-3">{events.toReversed().map(event => <li key={event.id} className="space-y-2 rounded-lg border p-3 text-sm">
    <time className="text-xs text-muted-foreground" dateTime={event.ts}>{new Date(event.ts).toLocaleTimeString()}</time>
    {event.type === "supervisor.action" && <><h3 className="font-medium">{event.payload.tool}</h3><p className="whitespace-pre-wrap break-words">Why: {event.payload.why}</p>{event.payload.result && <p className="whitespace-pre-wrap break-words">{event.payload.result}</p>}</>}
    {event.type === "supervisor.triggered" && <><h3 className="font-medium">{event.payload.reason.replaceAll("_", " ")}</h3><p className="whitespace-pre-wrap break-words">{event.payload.detail}</p></>}
  </li>)}</ol> : <p className="text-sm text-muted-foreground">No supervisor actions yet.</p>;
}
