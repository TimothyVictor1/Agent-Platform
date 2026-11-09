import type { Actor, Agent, BoardEntry, Event, Frame } from "@/contracts/generated";

export const LANE_HEIGHT = 124;
export const SUPERVISOR = "supervisor";
export type Lane = Pick<Agent, "id" | "name" | "color">;
export type Span = { id: string; lane: string; start: number; end: number; label: string; event: Event; kind: "task" | "takeover" };
export const boardSymbols: Record<BoardEntry["kind"], string> = {
  fact: "◆", claim: "⚑", question: "?", answer: "↩", decision: "✓", blocker: "!", progress: "…", artifact: "▣", note: "≡",
};
export const time = (item: { ts: string }) => Date.parse(item.ts);
export const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));
export function timeToX(ts: number, start: number, end: number, width: number) {
  return end <= start ? 0 : clamp((ts - start) / (end - start), 0, 1) * width;
}
export function xToTime(x: number, start: number, end: number, width: number) {
  return start + clamp(x / Math.max(1, width), 0, 1) * (end - start);
}
/** Upper-bound lookup handles simultaneous timestamps deterministically. */
export function upperBound<T>(items: readonly T[], value: number, key: (item: T) => number): number {
  let low = 0, high = items.length;
  while (low < high) { const mid = (low + high) >>> 1; if (key(items[mid]) <= value) low = mid + 1; else high = mid; }
  return low;
}
export function frameAt(frames: readonly Frame[], ts: number) {
  return frames[upperBound(frames, ts, time) - 1] ?? null;
}
export const actorLane = (actor: Actor) => actor.type === "agent" && actor.id ? actor.id : SUPERVISOR;
export function eventLane(event: Event): string {
  if (event.type === "board.entry_posted") return actorLane(event.payload.entry.author);
  if (event.type === "approval.requested" || event.type === "approval.decided") return event.payload.approval.agent_id;
  if (event.type === "agent.created") return event.payload.agent.id;
  if (event.actor.type === "supervisor") return SUPERVISOR;
  if ("agent_id" in event.payload && typeof event.payload.agent_id === "string") return event.payload.agent_id;
  return actorLane(event.actor);
}
export function recipients(event: Event): string[] {
  if (event.type === "board.entry_posted") return event.payload.entry.to ?? [];
  if (event.type === "user.steer" && event.payload.agent_id) return [event.payload.agent_id];
  return [];
}
export function arrowEndpoints(x: number, source: number, target: number) {
  const y1 = source * LANE_HEIGHT + 24, y2 = target * LANE_HEIGHT + 24;
  // Equal timestamp at both ends; the curve bends forward in time.
  return { x1: x, y1, x2: x, y2, path: `M ${x} ${y1} C ${x + 44} ${y1}, ${x + 44} ${y2}, ${x} ${y2}` };
}
export function eventLabel(event: Event) {
  if (event.type === "board.entry_posted") return `${event.payload.entry.kind}: ${event.payload.entry.title}`;
  if (event.type === "agent.step") return `Step ${event.payload.step}: ${event.payload.action?.tool ?? "reasoning"}`;
  return event.type;
}

export function buildReplay(eventsInput: readonly Event[], frameInput: readonly Frame[], agents: readonly Lane[] = []) {
  const events = [...eventsInput].sort((a, b) => time(a) - time(b) || a.seq - b.seq);
  const laneMap = new Map(agents.map(agent => [agent.id, agent]));
  for (const event of events) if (event.type === "agent.created") laneMap.set(event.payload.agent.id, event.payload.agent);
  const frames = new Map(frameInput.map(frame => [frame.id, frame]));
  for (const event of events) if (event.type === "agent.step" && event.payload.frame) frames.set(event.payload.frame.id, event.payload.frame);
  const ensureLane = (id: string) => { if (id !== SUPERVISOR && !laneMap.has(id)) laneMap.set(id, { id, name: id, color: "#64748b" }); };
  for (const event of events) { ensureLane(eventLane(event)); recipients(event).forEach(ensureLane); }
  const framesByAgent = new Map<string, Frame[]>();
  for (const frame of frames.values()) { ensureLane(frame.agent_id); const group = framesByAgent.get(frame.agent_id) ?? []; group.push(frame); framesByAgent.set(frame.agent_id, group); }
  for (const group of framesByAgent.values()) group.sort((a, b) => time(a) - time(b) || a.step - b.step);
  const timestamps = [...events.map(time), ...Array.from(frames.values(), time)];
  const start = timestamps.length ? Math.min(...timestamps) : 0;
  const end = timestamps.length ? Math.max(...timestamps) : start;
  const spans: Span[] = [];
  const tasks = new Map<string, Event & { type: "task.started" }>();
  const takeovers = new Map<string, Event & { type: "user.takeover_started" }>();
  const closeTask = (key: string, ts: number) => {
    const event = tasks.get(key); if (!event) return;
    spans.push({ id: event.id, lane: event.payload.agent_id, start: time(event), end: ts, label: event.payload.task_key, event, kind: "task" }); tasks.delete(key);
  };
  const closeTakeover = (id: string, ts: number) => {
    const event = takeovers.get(id); if (!event) return;
    spans.push({ id: event.id, lane: id, start: time(event), end: ts, label: "Human control", event, kind: "takeover" }); takeovers.delete(id);
  };
  for (const event of events) {
    if (event.type === "task.started") { closeTask(event.payload.task_id, time(event)); tasks.set(event.payload.task_id, event); }
    if (["task.accepted", "task.result_submitted", "task.failed", "task.cancelled"].includes(event.type) && "task_id" in event.payload) closeTask(event.payload.task_id, time(event));
    if (event.type === "user.takeover_started") { closeTakeover(event.payload.agent_id, time(event)); takeovers.set(event.payload.agent_id, event); }
    if (event.type === "user.takeover_ended") closeTakeover(event.payload.agent_id, time(event));
  }
  for (const id of tasks.keys()) closeTask(id, end);
  for (const id of takeovers.keys()) closeTakeover(id, end);
  const lanes = [...laneMap.values(), { id: SUPERVISOR, name: "Supervisor", color: "#f59e0b" }];
  const byType = new Map<Event["type"], Event[]>();
  for (const event of events) { const group = byType.get(event.type) ?? []; group.push(event); byType.set(event.type, group); }
  const maxFrames = Math.max(0, ...Array.from(framesByAgent.values(), group => group.length));
  return { events, lanes, framesByAgent, start, end, spans, byType, width: Math.max(1200, maxFrames * 108) };
}
export type ReplayModel = ReturnType<typeof buildReplay>;

export type Playback = { position: number; playing: boolean; speed: 1 | 4 | 16 };
export type PlaybackAction = { type: "toggle"; end: number; start: number } | { type: "seek"; position: number; start: number; end: number } | { type: "tick"; elapsed: number; end: number } | { type: "speed"; speed: Playback["speed"] };
export function playbackReducer(state: Playback, action: PlaybackAction): Playback {
  switch (action.type) {
    case "toggle": return action.end <= action.start ? state : { ...state, position: state.position >= action.end ? action.start : state.position, playing: !state.playing };
    case "seek": return { ...state, position: clamp(action.position, action.start, action.end), playing: false };
    case "speed": return { ...state, speed: action.speed };
    case "tick": {
      if (!state.playing) return state;
      const position = Math.min(action.end, state.position + Math.max(0, action.elapsed) * state.speed);
      return { ...state, position, playing: position < action.end };
    }
  }
}
