"use client";

import Link from "next/link";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useStore } from "zustand";
import { AlertDialog, Dialog } from "radix-ui";
import type { AgentStepEvent, Event, Session, WsThumbnailMessage } from "@/contracts/generated";
import { api } from "@/lib/api";
import { createSessionStore } from "@/lib/session-store";
import { SessionSocket, type ConnectionState } from "@/lib/ws";
import { CostMeter } from "@/components/shared/cost-meter";
import { StatusChip } from "@/components/shared/status-chip";
import { Button } from "@/components/ui/button";
import { AgentTile, FocusedAgent } from "./agent-views";
import { ArtifactDownload, CommandFeedback, useCommand } from "./common";
import { LivePanel } from "./live-panel";
import { SteerForm } from "./steer-form";

async function loadSession(sessionId: string) {
  const detail = await api.session(sessionId);
  // SessionDetail omits board, approvals and activity. Rebuild them with the same
  // reducer as the socket, stopping at the snapshot's cursor before subscribing.
  const events: Event[] = [];
  let cursor = 0;
  while (cursor < detail.last_seq) {
    const page = await api.events(sessionId, { after_seq: cursor, limit: 200 });
    const batch = page.items.filter(event => event.seq > cursor && event.seq <= detail.last_seq);
    const next = batch.at(-1)?.seq;
    if (next === undefined || next <= cursor) throw new Error("Could not load session history. Retry to resume.");
    events.push(...batch);
    cursor = next;
  }
  return { detail, events };
}

export function LiveSession({ sessionId }: { sessionId: string }) {
  // Route changes always get an isolated projection and socket lifecycle.
  return <SessionWorkspace key={sessionId} sessionId={sessionId} />;
}

function SessionWorkspace({ sessionId }: { sessionId: string }) {
  const [resources] = useState(() => ({ store: createSessionStore(sessionId) }));
  const hydrated = useRef(false);
  const state = useStore(resources.store);
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [thumbnails, setThumbnails] = useState<Record<string, WsThumbnailMessage>>({});
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [messageAgentId, setMessageAgentId] = useState<string | null>(null);
  const [messageOpen, setMessageOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const query = useQuery({ queryKey: ["live-session", sessionId], queryFn: () => loadSession(sessionId) });

  // REST hydration is independent of the lifetime of the socket.
  useEffect(() => {
    if (!query.data) return;
    for (const event of query.data.events) resources.store.getState().receive(event);
    hydrated.current = true;
    resources.store.getState().hydrate(query.data.detail);
  }, [query.data, resources]);

  const attachSocket = useEffectEvent((socket: SessionSocket) => {
    let connected = false;
    const start = () => {
      if (connected || !hydrated.current) return;
      connected = true;
      socket.connect(sessionId, resources.store.getState().lastSeq);
    };
    const unsubscribe = resources.store.subscribe(start);
    const offEvent = socket.onEvent(event => {
      const previous = resources.store.getState();
      resources.store.getState().receive(event);
      if (event.seq > previous.lastSeq && event.type === "approval.requested" && event.payload.approval.status === "pending" && !previous.approvals[event.payload.approval.id] && "Notification" in window && Notification.permission === "granted") {
        try { new Notification("Approval requested", { body: event.payload.approval.action_summary, tag: event.payload.approval.id }); }
        catch { /* In-app approval cards remain available if the browser blocks notifications. */ }
      }
    });
    const offThumbnail = socket.onThumbnail(thumbnail => setThumbnails(current => current[thumbnail.agent_id]?.step > thumbnail.step ? current : { ...current, [thumbnail.agent_id]: thumbnail }));
    const offState = socket.onState(setConnection);
    start();
    return () => { unsubscribe(); offEvent(); offThumbnail(); offState(); };
  });

  // Only the session id owns this effect. Query refetches never reopen a socket.
  useEffect(() => {
    const socket = new SessionSocket();
    const detach = attachSocket(socket);
    return () => { detach(); socket.disconnect(); };
  }, [sessionId]);

  const agents = Object.values(state.agents);
  const focused = focusedId ? state.agents[focusedId] : undefined;
  const terminal = state.status === "completed" || state.status === "stopped" || state.status === "failed";
  const panel = <LivePanel sessionId={sessionId} state={state} />;
  return <main className="mx-auto w-full max-w-[1800px] space-y-5 px-4 py-6 pb-24 lg:px-6 lg:pb-6">
    <header className="space-y-4 rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-xs uppercase tracking-widest text-muted-foreground">Live session</p><h1 className="mt-1 text-2xl font-semibold">{state.session?.title || "Loading session…"}</h1></div>
        {state.session && <SessionControls session={state.session} />}</div>
      {state.session && <div className="grid gap-4 sm:grid-cols-[1fr_2fr]"><div className="flex flex-wrap items-center gap-3"><StatusChip status={state.session.status} /><ElapsedTime session={state.session} /></div><CostMeter usage={state.usage} budgets={state.session.budgets} /></div>}
      <nav aria-label="Session navigation" className="flex gap-4 text-sm"><Link className="underline" href={`/sessions/${encodeURIComponent(sessionId)}/plan`}>Plan</Link><Link className="underline" href={`/sessions/${encodeURIComponent(sessionId)}/replay`}>Replay</Link></nav>
    </header>
    {query.error && <div role="alert" className="rounded-lg border border-red-500 p-4">Could not load session: {query.error.message} <Button variant="outline" onClick={() => void query.refetch()}>Retry</Button></div>}
    {connection !== "connected" && <p role="status" className="rounded-lg border border-amber-500/50 bg-amber-500/10 p-3 text-sm">{connection === "connecting" ? "Connecting to session…" : `Disconnected · ${connection === "reconnecting" ? "Reconnecting automatically" : "Waiting to reconnect"}`} · Resume from event {state.lastSeq}.</p>}
    {state.status === "completed" && <div role="status" className="rounded-lg border border-emerald-500 bg-emerald-500/10 p-4">Session complete. <Link className="underline" href={`/sessions/${encodeURIComponent(sessionId)}/replay`}>View Replay</Link> · <a className="underline" href="#session-artifacts">View artifacts</a></div>}
    <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_380px] 2xl:grid-cols-[minmax(0,1fr)_420px]">
      <div className="min-w-0 space-y-5">
        {focused ? <FocusedAgent agent={focused} agents={agents} sessionId={sessionId} thumbnail={thumbnails[focused.id]} steps={state.activity.filter((event): event is AgentStepEvent => event.type === "agent.step" && event.payload.agent_id === focused.id)} board={state.board.filter(entry => entry.author.id === focused.id)} onClose={() => setFocusedId(null)} /> :
          <section aria-label="Agent grid" className="grid grid-cols-1 gap-4 xl:grid-cols-2 2xl:grid-cols-3">{!agents.length && <p className="rounded-lg border p-6 text-sm text-muted-foreground">Waiting for agents…</p>}{agents.map(agent => <AgentTile key={agent.id} agent={agent} terminal={terminal} thumbnail={thumbnails[agent.id]} task={agent.current_task_id ? state.tasks[agent.current_task_id] : undefined} onFocus={() => setFocusedId(agent.id)} onMessage={() => { setMessageAgentId(agent.id); setMessageOpen(true); }} />)}</section>}
        <section id="session-artifacts" aria-label="Session artifacts" className="space-y-2 rounded-xl border p-4"><h2 className="font-semibold">Artifacts</h2>{Object.values(state.artifacts).map(artifact => <ArtifactDownload key={artifact.id} artifactId={artifact.id} artifact={artifact} />)}{!Object.keys(state.artifacts).length && <p className="text-sm text-muted-foreground">No artifacts published yet.</p>}</section>
      </div>
      <aside className="sticky top-4 hidden max-h-[calc(100dvh-2rem)] overflow-auto rounded-xl border bg-card xl:block" aria-label="Session panel">{panel}</aside>
    </div>
    <Dialog.Root open={sheetOpen} onOpenChange={setSheetOpen}><Dialog.Trigger asChild><Button className="fixed inset-x-4 bottom-4 z-20 xl:hidden">Open session panel{Object.values(state.approvals).filter(approval => approval.status === "pending").length ? " · Approvals pending" : ""}</Button></Dialog.Trigger>
      <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 xl:hidden" /><Dialog.Content className="fixed inset-x-0 bottom-0 z-50 max-h-[85dvh] overflow-auto rounded-t-2xl border bg-background p-2 shadow-xl xl:hidden" aria-describedby={undefined}>
        <div className="flex items-center justify-between p-3"><Dialog.Title className="font-semibold">Session panel</Dialog.Title><Dialog.Close asChild><Button variant="ghost">Close panel</Button></Dialog.Close></div>{sheetOpen && panel}
      </Dialog.Content></Dialog.Portal>
    </Dialog.Root>
    <Dialog.Root open={messageOpen} onOpenChange={setMessageOpen}><Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-40 bg-black/50" /><Dialog.Content className="fixed top-1/2 left-1/2 z-50 max-h-[90dvh] w-[min(95vw,520px)] -translate-x-1/2 -translate-y-1/2 overflow-auto rounded-xl border bg-background p-4" aria-describedby={undefined}>
      <div className="mb-4 flex items-center justify-between"><Dialog.Title className="font-semibold">Steer session</Dialog.Title><Dialog.Close asChild><Button variant="ghost">Close message</Button></Dialog.Close></div>
      <SteerForm key={messageAgentId} sessionId={sessionId} agents={agents} initialAgentId={messageAgentId || ""} autoFocus />
    </Dialog.Content></Dialog.Portal></Dialog.Root>
  </main>;
}

function ElapsedTime({ session }: { session: Session }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!session.started_at || session.ended_at) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [session.started_at, session.ended_at]);
  const seconds = session.started_at ? Math.max(0, Math.floor(((session.ended_at ? Date.parse(session.ended_at) : now) - Date.parse(session.started_at)) / 1000)) : 0;
  return <span className="text-sm tabular-nums" aria-label="Elapsed time">{Math.floor(seconds / 3600).toString().padStart(2, "0")}:{Math.floor(seconds / 60 % 60).toString().padStart(2, "0")}:{(seconds % 60).toString().padStart(2, "0")}</span>;
}

function SessionControls({ session }: { session: Session }) {
  const [confirmStop, setConfirmStop] = useState(false);
  const command = useCommand((action: "stop" | "pause" | "resume") => api[action](session.id));
  const terminal = ["completed", "stopped", "failed"].includes(session.status);
  return <div className="space-y-2"><div className="flex gap-2">
    <Button variant="outline" disabled={command.isPending || !["running", "paused"].includes(session.status)} onClick={() => command.mutate(session.status === "paused" ? "resume" : "pause")}>{session.status === "paused" ? "Resume" : "Pause"}</Button>
    <AlertDialog.Root open={confirmStop} onOpenChange={setConfirmStop}><AlertDialog.Trigger asChild><Button variant="destructive" disabled={terminal || command.isPending}>Stop</Button></AlertDialog.Trigger>
      <AlertDialog.Portal><AlertDialog.Overlay className="fixed inset-0 z-50 bg-black/50" /><AlertDialog.Content className="fixed top-1/2 left-1/2 z-50 w-[min(95vw,440px)] -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-background p-6">
        <AlertDialog.Title className="text-lg font-semibold">Stop this session?</AlertDialog.Title><AlertDialog.Description className="text-sm text-muted-foreground">All agents will stop and their desktops will close. The replay will be saved.</AlertDialog.Description>
        <div className="flex justify-end gap-2"><AlertDialog.Cancel asChild><Button variant="outline">Keep running</Button></AlertDialog.Cancel><Button variant="destructive" disabled={command.isPending} onClick={() => command.mutate("stop", { onSuccess: () => setConfirmStop(false) })}>Confirm stop</Button></div>
        {command.error && <p role="alert">{command.error.message}</p>}
      </AlertDialog.Content></AlertDialog.Portal>
    </AlertDialog.Root>
  </div><CommandFeedback error={command.error} success={command.isSuccess} /></div>;
}
