"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { Event, Frame } from "@/contracts/generated";
import { FrameImage } from "./frame-image";
import { arrowEndpoints, boardSymbols, eventLabel, eventLane, LANE_HEIGHT, recipients, time, timeToX, upperBound, xToTime, type ReplayModel } from "./model";

export function Timeline({ model, position, onSelect, onFrame }: {
  model: ReplayModel; position: number; onSelect: (event: Event) => void; onFrame: (frame: Frame) => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const markerId = useId().replace(/:/g, "");
  const [viewport, setViewport] = useState({ left: 0, width: 900 });
  useEffect(() => {
    const element = scroll.current;
    if (!element) return;
    const update = () => setViewport({ left: element.scrollLeft, width: element.clientWidth || 900 });
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update); observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const x = timeToX(position, model.start, model.end, model.width);
  useEffect(() => {
    const element = scroll.current;
    if (element && (x < element.scrollLeft || x > element.scrollLeft + (element.clientWidth || 900) - 100)) {
      element.scrollLeft = Math.max(0, x - (element.clientWidth || 900) / 2);
      setViewport({ left: element.scrollLeft, width: element.clientWidth || 900 });
    }
  }, [x]);
  const window = useMemo(() => {
    const left = Math.max(0, viewport.left - 160), right = viewport.left + viewport.width + 160;
    return { left, right, start: xToTime(left, model.start, model.end, model.width), end: xToTime(right, model.start, model.end, model.width) };
  }, [viewport, model]);
  const visibleEvents = useMemo(() => model.events.slice(
    upperBound(model.events, window.start - 1, time), upperBound(model.events, window.end, time),
  ).filter(event => event.type !== "agent.step"), [model, window]);
  const laneIndex = new Map(model.lanes.map((lane, i) => [lane.id, i]));
  const px = (ts: number) => timeToX(ts, model.start, model.end, model.width);

  return <section aria-label="Replay timeline" className="rounded-xl border bg-card">
    <div className="border-b p-3 text-xs text-muted-foreground">Scroll to explore · Select a frame or event · ◆ fact · ⚑ claim · ? question · ↩ answer · ✓ decision · ! blocker · … progress · ▣ artifact · ≡ note · ⛨ approval · ⇄ takeover</div>
    <div className="flex min-w-0">
      <div className="w-28 shrink-0 border-r pt-6">{model.lanes.map(lane => <div key={lane.id} className="border-b p-3 text-sm font-medium" style={{ height: LANE_HEIGHT, borderLeft: `4px solid ${lane.color}` }}>{lane.name}</div>)}</div>
      <div ref={scroll} tabIndex={0} role="region" aria-label="Scrollable replay lanes" className="min-w-0 flex-1 overflow-x-auto focus-visible:outline-2" onScroll={event => setViewport({ left: event.currentTarget.scrollLeft, width: event.currentTarget.clientWidth || 900 })}>
        <div className="relative" style={{ width: model.width + 100, height: model.lanes.length * LANE_HEIGHT + 24 }}>
          <div className="h-6 text-xs text-muted-foreground">{Array.from({ length: Math.ceil(viewport.width / 200) + 2 }, (_, i) => Math.floor(viewport.left / 200) * 200 + i * 200).filter(tick => tick <= model.width).map(tick => <span key={tick} className="absolute" style={{ left: tick }}>{((xToTime(tick, model.start, model.end, model.width) - model.start) / 1000).toFixed(1)}s</span>)}</div>
          {model.lanes.map((lane, index) => {
            const frames = model.framesByAgent.get(lane.id) ?? [];
            const visible = frames.slice(upperBound(frames, window.start - 1, time), upperBound(frames, window.end, time));
            return <div key={lane.id} aria-label={`${lane.name} lane`} className="absolute w-full border-b bg-muted/10" style={{ top: 24 + index * LANE_HEIGHT, height: LANE_HEIGHT, borderTop: `2px solid ${lane.color}` }}>
              {visible.map(frame => <button key={frame.id} data-testid="timeline-thumbnail" aria-label={`${lane.name}, step ${frame.step}: ${frame.action_label}`} className="absolute top-10 h-16 w-24 rounded border focus-visible:outline-2" style={{ left: px(time(frame)) }} onClick={() => onFrame(frame)}><span className="sr-only">Step {frame.step}</span><FrameImage frame={frame} label={`${lane.name} step ${frame.step}`} /></button>)}
            </div>;
          })}
          {model.spans.filter(span => span.end >= window.start && span.start <= window.end).map(span => <button key={span.id} aria-label={`${span.kind}: ${span.label}`} title={span.label} onClick={() => onSelect(span.event)} className={`absolute h-4 truncate rounded px-1 text-left text-[10px] focus-visible:outline-2 ${span.kind === "takeover" ? "bg-amber-500/30" : "bg-sky-500/20"}`} style={{ left: Math.max(window.left, px(span.start)), width: Math.max(6, Math.min(window.right, px(span.end)) - Math.max(window.left, px(span.start))), top: 24 + (laneIndex.get(span.lane) ?? 0) * LANE_HEIGHT + (span.kind === "task" ? 106 : 88) }}>{span.label}</button>)}
          <svg aria-label="Message arrows" className="pointer-events-none absolute top-6 overflow-visible" style={{ left: window.left }} viewBox={`${window.left} 0 ${window.right - window.left + 100} ${model.lanes.length * LANE_HEIGHT}`} width={window.right - window.left + 100} height={model.lanes.length * LANE_HEIGHT}>
            <defs><marker id={markerId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
            {visibleEvents.flatMap(event => recipients(event).map(target => {
              const from = laneIndex.get(eventLane(event)), to = laneIndex.get(target);
              if (from === undefined || to === undefined) return null;
              return <path key={`${event.id}-${target}`} aria-label={`Message from ${model.lanes[from].name} to ${model.lanes[to].name}`} d={arrowEndpoints(px(time(event)), from, to).path} fill="none" stroke="currentColor" strokeWidth="1.5" markerEnd={`url(#${markerId})`} />;
            }))}
          </svg>
          {visibleEvents.map(event => <button key={event.id} title={`${eventLabel(event)} · #${event.seq}`} aria-label={`${eventLabel(event)} · #${event.seq}`} className="absolute z-10 grid size-6 place-items-center rounded border bg-background text-xs shadow-sm focus-visible:outline-2" style={{ left: px(time(event)) - 12, top: 24 + (laneIndex.get(eventLane(event)) ?? model.lanes.length - 1) * LANE_HEIGHT + 12 }} onClick={() => onSelect(event)}>
            {event.type === "board.entry_posted" ? boardSymbols[event.payload.entry.kind] : event.type.startsWith("approval.") ? "⛨" : event.type.startsWith("user.takeover") ? "⇄" : event.type.startsWith("task.") ? "▰" : "•"}
          </button>)}
          <div aria-hidden="true" className="pointer-events-none absolute top-0 bottom-0 z-20 w-px bg-foreground" style={{ left: x }}><span className="absolute -left-1 top-0 size-2 rotate-45 bg-foreground" /></div>
        </div>
      </div>
    </div>
  </section>;
}
