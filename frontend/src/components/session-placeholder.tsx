"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useStore } from "zustand";
import { api } from "@/lib/api";
import { createSessionStore } from "@/lib/session-store";
import { SessionSocket, type ConnectionState } from "@/lib/ws";
import { CostMeter } from "@/components/shared/cost-meter";
import { EmptyState } from "@/components/shared/empty-state";
import { StatusChip } from "@/components/shared/status-chip";
import { AgentAvatar } from "@/components/shared/agent-avatar";

/** Foundation only; the plan editor, live controls and replay timeline belong to later tasks. */
export function SessionPlaceholder({ sessionId, title }: { sessionId: string; title: string }) {
  const [store] = useState(() => createSessionStore(sessionId));
  const state = useStore(store);
  const [connection, setConnection] = useState<ConnectionState>("disconnected");
  const detail = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId) });
  useEffect(() => {
    if (!detail.data) return;
    store.getState().hydrate(detail.data);
  }, [detail.data, store]);
  useEffect(() => {
    if (!detail.isSuccess) return;
    const socket = new SessionSocket();
    socket.onEvent(event => store.getState().receive(event));
    socket.onState(setConnection);
    socket.connect(sessionId, store.getState().lastSeq);
    return () => socket.disconnect();
  }, [sessionId, store, detail.isSuccess]);
  return <main className="mx-auto w-full max-w-6xl space-y-6 px-6 py-10"><div><h1 className="text-3xl font-semibold">{title}</h1><p className="mt-2 text-muted-foreground">{state.session?.title || "Loading session…"}</p></div>
    <nav aria-label="Session navigation" className="flex gap-5 text-sm">{[["plan", "Plan"], ["", "Live session"], ["replay", "Replay"]].map(([route, label]) => <Link className="underline" key={label} href={`/sessions/${encodeURIComponent(sessionId)}${route ? `/${route}` : ""}`}>{label}</Link>)}</nav>
    {detail.error && <p role="alert">Could not load session: {detail.error.message}</p>}
    <p role="status" className="text-sm text-muted-foreground">Connection: {connection}{connection === "reconnecting" ? " · Reconnecting from the latest event…" : ""}</p>
    {state.status && <StatusChip status={state.status} />}
    {state.session && <CostMeter usage={state.usage} budgets={state.session.budgets} />}
    <div className="flex gap-4">{Object.values(state.agents).map(agent => <AgentAvatar key={agent.id} name={agent.name} color={agent.color} />)}</div>
    <EmptyState title={`${title} workspace`}>The session foundation is ready. This workspace’s editing and visualization tools will be added in a later task.</EmptyState>
  </main>;
}
