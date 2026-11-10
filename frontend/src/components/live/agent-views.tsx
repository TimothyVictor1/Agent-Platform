"use client";

import { useState } from "react";
import type { Agent, AgentStepEvent, BoardEntry, Task, WsThumbnailMessage } from "@/contracts/generated";
import { api } from "@/lib/api";
import { AgentAvatar } from "@/components/shared/agent-avatar";
import { StatusChip } from "@/components/shared/status-chip";
import { Button } from "@/components/ui/button";
import { AgentStream } from "./agent-stream";
import { BoardEntries } from "./board-panel";
import { CommandFeedback, useCommand } from "./common";
import { SteerForm } from "./steer-form";

export function AgentTile({ agent, task, thumbnail, terminal, onFocus, onMessage }: { agent: Agent; task?: Task; thumbnail?: WsThumbnailMessage; terminal: boolean; onFocus: () => void; onMessage: () => void }) {
  const takeover = useCommand(() => api.takeover(agent.id));
  const release = useCommand(() => api.release(agent.id));
  const controlling = agent.status === "paused_by_user";
  return <article aria-label={`${agent.name} agent`} className="min-w-0 space-y-3 rounded-xl border-t-4 bg-card p-4 shadow-sm" style={{ borderTopColor: agent.color }}>
    <div className="flex flex-wrap items-center justify-between gap-2"><AgentAvatar name={agent.name} color={agent.color} /><StatusChip status={agent.status} /></div>
    {controlling && <TakeoverBanner name={agent.name} pending={release.isPending} onRelease={() => release.mutate(undefined)} />}
    <AgentStream agent={agent} thumbnail={thumbnail} interactive={controlling} />
    <p className="text-sm">{task?.title || "No current task"}</p><p className="text-xs text-muted-foreground">Step {agent.step}</p>
    <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={onFocus} aria-label={`Focus ${agent.name}`}>Focus</Button>
      <Button variant="outline" onClick={onMessage} aria-label={`Message ${agent.name}`}>Message</Button>
      <Button variant="outline" disabled={terminal || controlling || takeover.isPending || !agent.interactive_stream_url} onClick={() => takeover.mutate(undefined)} aria-label={`Take over ${agent.name}`}>Take over</Button></div>
    <CommandFeedback error={takeover.error || release.error} success={takeover.isSuccess || release.isSuccess} />
  </article>;
}

function TakeoverBanner({ name, pending, onRelease }: { name: string; pending: boolean; onRelease: () => void }) {
  return <div role="status" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-600 bg-red-600 p-3 text-sm text-white">
    <p>You are controlling {name}&apos;s desktop</p><Button variant="secondary" disabled={pending} onClick={onRelease}>Return control</Button>
  </div>;
}

export function FocusedAgent({ agent, agents, sessionId, steps, board, thumbnail, onClose }: { agent: Agent; agents: Agent[]; sessionId: string; steps: AgentStepEvent[]; board: BoardEntry[]; thumbnail?: WsThumbnailMessage; onClose: () => void }) {
  const release = useCommand(() => api.release(agent.id));
  return <section aria-label={`${agent.name} focused view`} className="space-y-4 rounded-xl border p-4" style={{ borderColor: agent.color }}>
    <div className="flex items-center justify-between"><h2 className="text-xl font-semibold">{agent.name}&apos;s desktop</h2><Button variant="outline" onClick={onClose}>Back to agent grid</Button></div>
    {agent.status === "paused_by_user" && <TakeoverBanner name={agent.name} pending={release.isPending} onRelease={() => release.mutate(undefined)} />}
    <CommandFeedback error={release.error} success={release.isSuccess} />
    <AgentStream agent={agent} thumbnail={thumbnail} interactive={agent.status === "paused_by_user"} />
    <h3 className="font-medium">Step timeline</h3><StepTimeline key={`timeline-${agent.id}`} steps={steps} />
    <h3 className="font-medium">{agent.name}&apos;s board activity</h3><BoardEntries entries={board} agents={agents} />
    <SteerForm key={agent.id} sessionId={sessionId} agents={agents} initialAgentId={agent.id} />
  </section>;
}

// Fixed-height rows keep the DOM bounded while preserving the full scroll range.
export function StepTimeline({ steps }: { steps: AgentStepEvent[] }) {
  const [scrollTop, setScrollTop] = useState(0);
  const rowHeight = 128;
  const start = Math.max(0, Math.floor(scrollTop / rowHeight) - 2);
  const visible = steps.slice(start, start + 8);
  if (!steps.length) return <p className="text-sm text-muted-foreground">No steps yet.</p>;
  return <div aria-label="Step timeline" role="list" tabIndex={0} className="h-96 overflow-auto rounded-lg border" onScroll={event => setScrollTop(event.currentTarget.scrollTop)}>
    <div className="relative" style={{ height: steps.length * rowHeight }}>
      {visible.map((event, index) => <div key={event.id} role="listitem" aria-posinset={start + index + 1} aria-setsize={steps.length} className="absolute inset-x-0 space-y-1 overflow-hidden border-b p-3 text-sm" style={{ top: (start + index) * rowHeight, height: rowHeight }}>
        <p className="flex justify-between gap-3"><strong>Step {event.payload.step} · {event.payload.action?.tool || "Observe"}</strong><time dateTime={event.ts}>{new Date(event.ts).toLocaleTimeString()}</time></p>
        <p className="truncate">{event.payload.action ? JSON.stringify(event.payload.action.args) : "No desktop action"}</p>
        <p className="line-clamp-2 whitespace-pre-wrap break-words text-muted-foreground">{event.payload.reasoning_excerpt}</p>
      </div>)}
    </div>
  </div>;
}
