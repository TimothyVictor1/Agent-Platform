"use client";

import Link from "next/link";
import { useEffect, useMemo, useReducer, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { Event, Frame } from "@/contracts/generated";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { loadEvents } from "./data";
import { EventDetails } from "./event-details";
import { artifactsZip, download } from "./exports";
import { FrameImage } from "./frame-image";
import { buildReplay, eventLane, frameAt, playbackReducer, SUPERVISOR, time, upperBound, type Playback } from "./model";
import { Timeline } from "./timeline";

export function ReplayPage({ sessionId }: { sessionId: string }) {
  const detail = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId) });
  const log = useQuery({ queryKey: ["replay-events", sessionId], queryFn: ({ signal }) => loadEvents(sessionId, signal) });
  const frames = useQuery({ queryKey: ["replay-frames", sessionId], queryFn: () => api.frames(sessionId) });
  const model = useMemo(() => buildReplay(log.data ?? [], frames.data?.items ?? [], detail.data?.agents ?? []), [log.data, frames.data, detail.data]);
  const error = detail.error ?? log.error ?? frames.error;
  const loading = detail.isPending || log.isPending || frames.isPending;
  return <main className="mx-auto w-full max-w-[1800px] space-y-5 px-4 py-6 md:px-6">
    <header><Link className="text-sm text-muted-foreground underline" href={`/sessions/${encodeURIComponent(sessionId)}`}>Back to session</Link><h1 className="mt-2 text-3xl font-semibold">Session replay</h1><p className="mt-1 text-muted-foreground">{detail.data?.session.title ?? sessionId}</p></header>
    {error ? <div role="alert" className="flex items-center gap-3">Could not load replay: {error.message}<Button variant="outline" onClick={() => { void detail.refetch(); void log.refetch(); void frames.refetch(); }}>Retry replay</Button></div> : loading ? <p role="status">Loading events and frame index…</p> : <ReplayContent key={sessionId} sessionId={sessionId} model={model} />}
  </main>;
}

function ReplayContent({ sessionId, model }: { sessionId: string; model: ReturnType<typeof buildReplay> }) {
  const [state, dispatch] = useReducer(playbackReducer, { position: model.start, playing: false, speed: 1 });
  const [agentId, setAgentId] = useState(model.lanes.find(lane => lane.id !== SUPERVISOR)?.id ?? "");
  const [selected, setSelected] = useState<Event | null>(null);
  const [filter, setFilter] = useState<Event["type"] | "">("");
  const position = Math.min(model.end, Math.max(model.start, state.position));
  const agents = model.lanes.filter(lane => lane.id !== SUPERVISOR);
  const agent = agents.find(item => item.id === agentId) ?? agents[0];
  const currentFrames = agents.map(lane => ({ lane, frame: frameAt(model.framesByAgent.get(lane.id) ?? [], position) }));
  const current = currentFrames.find(item => item.lane.id === agent?.id)?.frame;
  const seek = (ts: number) => dispatch({ type: "seek", position: ts, start: model.start, end: model.end });
  const selectEvent = (event: Event) => { setSelected(event); seek(time(event)); const lane = eventLane(event); if (lane !== SUPERVISOR) setAgentId(lane); };
  const step = (direction: -1 | 1) => {
    // Sequence, rather than timestamp alone, lets users inspect simultaneous events.
    const exact = selected && time(selected) === position ? model.events.indexOf(selected) : -1;
    const index = exact >= 0 ? exact + direction : direction > 0 ? upperBound(model.events, position, time) : upperBound(model.events, position - 1, time) - 1;
    const event = model.events[index]; if (event) selectEvent(event);
  };
  const toggle = () => dispatch({ type: "toggle", start: model.start, end: model.end });
  useEffect(() => {
    if (!state.playing) return;
    let previous = performance.now();
    const timer = window.setInterval(() => { const now = performance.now(); dispatch({ type: "tick", elapsed: now - previous, end: model.end }); previous = now; }, 50);
    return () => window.clearInterval(timer);
  }, [state.playing, model.end]);
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable], [role='textbox'], [role='slider']")) return;
      if (event.key === " " && event.target instanceof Element && event.target.closest("button, a, summary")) return;
      if (event.key === " ") { event.preventDefault(); if (!event.repeat) toggle(); }
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); step(event.key === "ArrowLeft" ? -1 : 1); }
    };
    window.addEventListener("keydown", handle); return () => window.removeEventListener("keydown", handle);
  });
  const zip = useMutation({ mutationFn: () => artifactsZip(sessionId), onSuccess: blob => download(blob, "artifacts.zip") });
  const candidates = filter ? model.byType.get(filter) ?? [] : model.events;
  const nextEvent = candidates.find(event => time(event) > position || time(event) === position && selected !== null && event.seq > selected.seq);
  const selectFrame = (frame: Frame) => {
    setAgentId(frame.agent_id); seek(time(frame));
    setSelected(model.byType.get("agent.step")?.find(event => event.type === "agent.step" && event.payload.agent_id === frame.agent_id && event.payload.step === frame.step) ?? null);
  };
  return <>
    <section aria-label="Playback controls" className="space-y-4 rounded-xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" onClick={() => step(-1)} disabled={!model.events.length || position <= model.start && (!selected || selected === model.events[0])}>Step back</Button>
        <Button onClick={toggle} disabled={model.end <= model.start}>{state.playing ? "Pause" : "Play"}</Button>
        <Button variant="outline" onClick={() => step(1)} disabled={!model.events.length || selected === model.events.at(-1)}>Step forward</Button>
        <label className="flex items-center gap-2">Speed<select className="w-auto" value={state.speed} onChange={event => dispatch({ type: "speed", speed: Number(event.target.value) as Playback["speed"] })}>{[1, 4, 16].map(speed => <option key={speed} value={speed}>{speed}×</option>)}</select></label>
        <span className="text-sm tabular-nums" aria-label="Playback time">{((position - model.start) / 1000).toFixed(1)} / {((model.end - model.start) / 1000).toFixed(1)}s</span>
        <div className="ml-auto flex flex-wrap gap-2"><Button variant="outline" onClick={() => download(new Blob([JSON.stringify(model.events, null, 2)], { type: "application/json" }), "events.json")}>Download events JSON</Button><Button variant="outline" disabled={zip.isPending} onClick={() => zip.mutate()}>{zip.isPending ? "Preparing ZIP…" : "Download artifacts ZIP"}</Button></div>
      </div>
      {zip.error && <p role="alert">Artifact export failed: {zip.error.message}</p>}
      <label className="block">Playhead<input type="range" min={0} max={Math.max(0, model.end - model.start)} step={1} value={position - model.start} disabled={model.end <= model.start} onChange={event => { seek(model.start + Number(event.target.value)); setSelected(null); }} /></label>
      <div className="flex flex-wrap items-center gap-3"><label className="flex items-center gap-2">Event type<select className="w-auto max-w-64" value={filter} onChange={event => setFilter(event.target.value as Event["type"] | "")}><option value="">All events</option>{[...model.byType.keys()].map(type => <option key={type} value={type}>{type}</option>)}</select></label><Button variant="outline" disabled={!nextEvent} onClick={() => { if (nextEvent) selectEvent(nextEvent); }}>Next event</Button><p className="text-xs text-muted-foreground">Space: play/pause · ←/→: step · {model.events.length} events</p></div>
    </section>
    {!model.events.length && <p role="status">No events have been recorded for this session.</p>}
    <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
      <section aria-label="Frame viewer" className="min-w-0 space-y-3 rounded-xl border bg-card p-4">
        <label className="flex items-center gap-3">Selected agent<select value={agent?.id ?? ""} className="max-w-64" disabled={!agents.length} onChange={event => setAgentId(event.target.value)}>{!agents.length && <option value="">No agents</option>}{agents.map(lane => <option key={lane.id} value={lane.id}>{lane.name}</option>)}</select></label>
        <div className="flex aspect-video items-center justify-center rounded-lg border bg-muted/20">{current ? <FrameImage key={current.id} frame={current} label={`${agent?.name} at step ${current.step}: ${current.action_label}`} /> : <p className="p-6 text-sm text-muted-foreground">No saved frame at this moment.</p>}</div>
        <div aria-label="All agents at playhead" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">{currentFrames.map(({ lane, frame }) => <button key={lane.id} aria-label={`View ${lane.name}`} aria-pressed={agent?.id === lane.id} className="space-y-1 rounded border p-2 text-left text-xs focus-visible:outline-2" style={{ borderColor: lane.color }} onClick={() => setAgentId(lane.id)}><span className="block truncate">{lane.name}{frame ? ` · ${frame.step}` : ""}</span><div className="aspect-video">{frame ? <FrameImage key={frame.id} frame={frame} label={`${lane.name} current frame`} /> : <span className="text-muted-foreground">No frame</span>}</div></button>)}</div>
      </section>
      <EventDetails event={selected} />
    </div>
    <Timeline model={model} position={position} onSelect={selectEvent} onFrame={selectFrame} />
  </>;
}
