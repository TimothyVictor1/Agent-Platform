"use client";

import { useRef, useEffect, useState } from "react";
import type { Agent, SteerRequest } from "@/contracts/generated";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CommandFeedback, useCommand } from "./common";

export function SteerForm({ sessionId, agents, initialAgentId = "", autoFocus = false }: { sessionId: string; agents: Agent[]; initialAgentId?: string; autoFocus?: boolean }) {
  const [agentId, setAgentId] = useState(initialAgentId);
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const command = useCommand((body: SteerRequest) => api.steer(sessionId, body));
  useEffect(() => { if (autoFocus) input.current?.focus(); }, [autoFocus]);
  return <form aria-label="Steer agents" className="space-y-3 rounded-lg border p-4" onSubmit={event => {
    event.preventDefault();
    if (!text.trim() || command.isPending) return;
    command.mutate({ agent_id: agentId || null, text: text.trim() }, { onSuccess: () => setText("") });
  }}>
    <h3 className="font-medium">Message your agents</h3>
    <label className="block">Message recipient<select value={agentId} onChange={event => setAgentId(event.target.value)}>
      <option value="">All agents and supervisor</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
    </select></label>
    <label className="block">Steering message<textarea ref={input} value={text} maxLength={1500} rows={3} onChange={event => setText(event.target.value)} required /></label>
    <Button type="submit" disabled={!text.trim() || command.isPending}>Send message</Button>
    <CommandFeedback error={command.error} success={command.isSuccess} />
  </form>;
}
