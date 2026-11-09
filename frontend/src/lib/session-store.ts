import { createStore } from "zustand/vanilla";
import type { Agent, Approval, Artifact, BoardEntry, Event, Heartbeat, Plan, Session, SessionDetail, Task, Usage } from "@/contracts/generated";

export const emptyUsage = (): Usage => ({ steps: 0, input_tokens: 0, output_tokens: 0, images: 0, cost_usd: 0 });
export interface SessionState {
  sessionId: string | null;
  session: Session | null;
  status: Session["status"] | null;
  plan: Plan | null;
  tasks: Record<string, Task>;
  agents: Record<string, Agent>;
  board: BoardEntry[];
  approvals: Record<string, Approval>;
  artifacts: Record<string, Artifact>;
  supervisorFeed: Event[];
  activity: Event[];
  usage: Usage;
  lastSeq: number;
}
export function initialSessionState(sessionId: string | null = null): SessionState {
  return { sessionId, session: null, status: null, plan: null, tasks: {}, agents: {}, board: [], approvals: {}, artifacts: {}, supervisorFeed: [], activity: [], usage: emptyUsage(), lastSeq: 0 };
}
function unreachable(event: never): never { throw new Error(`Unhandled event: ${JSON.stringify(event)}`); }

/** Immutable, ordered projection of the generated event union. Unknown additions fail typecheck. */
export function applyEvent(state: SessionState, event: Event): SessionState {
  if ((state.sessionId !== null && event.session_id !== state.sessionId) || event.seq <= state.lastSeq) return state;
  let next: SessionState = { ...state, sessionId: event.session_id, lastSeq: event.seq, activity: [...state.activity, event] };
  const task = (id: string, patch: Partial<Task>) => {
    if (next.tasks[id]) next = { ...next, tasks: { ...next.tasks, [id]: { ...next.tasks[id], ...patch } } };
  };
  const agent = (id: string, patch: Partial<Agent>) => {
    if (next.agents[id]) next = { ...next, agents: { ...next.agents, [id]: { ...next.agents[id], ...patch } } };
  };
  const status = (value: Session["status"]) => { next.status = value; };
  switch (event.type) {
    case "session.created": next.session = event.payload.session; next.status = next.session.status; next.usage = { ...next.session.usage }; break;
    case "session.planning_started": status("planning"); break;
    case "session.plan_proposed":
    case "session.plan_updated":
      next.plan = { session_id: event.session_id, version: event.payload.plan_version, proposal: event.payload.proposal, created_at: event.ts, approved_at: null };
      status("awaiting_approval");
      if (next.session) next.session = { ...next.session, plan_version: event.payload.plan_version };
      break;
    case "session.approved":
      status("running");
      if (next.plan) next.plan = { ...next.plan, approved_at: event.ts };
      if (next.session) next.session = { ...next.session, plan_version: event.payload.plan_version, started_at: next.session.started_at ?? event.ts };
      break;
    case "session.paused": status("paused"); break;
    case "session.resumed": status("running"); break;
    case "session.completed": status("completed"); break;
    case "session.planning_failed":
    case "session.failed": status("failed"); break;
    case "session.stopped": status("stopped"); break;
    // Budget events announce limits; session.stopped is the authoritative terminal transition.
    case "session.budget_warning":
    case "session.budget_exceeded": break;
    case "task.created":
    case "task.updated": next.tasks = { ...next.tasks, [event.payload.task.id]: event.payload.task }; break;
    case "task.ready": task(event.payload.task_id, { status: "ready" }); break;
    case "task.assigned": task(event.payload.task_id, { status: "assigned", assigned_agent_id: event.payload.agent_id, attempt: event.payload.attempt }); agent(event.payload.agent_id, { current_task_id: event.payload.task_id }); break;
    case "task.assignment_acked": task(event.payload.task_id, { assigned_agent_id: event.payload.agent_id, attempt: event.payload.attempt }); break;
    case "task.started": task(event.payload.task_id, { status: "running", assigned_agent_id: event.payload.agent_id, attempt: event.payload.attempt }); agent(event.payload.agent_id, { status: "working", current_task_id: event.payload.task_id }); break;
    case "task.result_submitted": task(event.payload.task_id, { result: event.payload.result, status: "in_review" }); break;
    case "task.review_started": task(event.payload.task_id, { status: "in_review" }); break;
    case "task.accepted": task(event.payload.task_id, { status: "done" }); break;
    case "task.rework_requested": task(event.payload.task_id, { status: "ready", assigned_agent_id: null, rework_count: event.payload.rework_count }); break;
    case "task.failed": task(event.payload.task_id, { status: event.payload.will_retry ? "ready" : "failed", attempt: event.payload.attempt, assigned_agent_id: event.payload.will_retry ? null : event.payload.agent_id }); break;
    case "task.cancelled": task(event.payload.task_id, { status: "cancelled" }); break;
    case "agent.created": next.agents = { ...next.agents, [event.payload.agent.id]: event.payload.agent }; break;
    case "agent.sandbox_booting": agent(event.payload.agent_id, { status: "booting" }); break;
    case "agent.sandbox_ready": agent(event.payload.agent_id, { status: "idle", sandbox: event.payload.sandbox, stream_url: event.payload.stream_url, interactive_stream_url: event.payload.interactive_stream_url }); break;
    case "agent.step": {
      agent(event.payload.agent_id, { step: event.payload.step, current_task_id: event.payload.task_id });
      const usage = event.payload.usage;
      next.usage = { steps: next.usage.steps + usage.steps, input_tokens: next.usage.input_tokens + usage.input_tokens, output_tokens: next.usage.output_tokens + usage.output_tokens, images: next.usage.images + usage.images, cost_usd: next.usage.cost_usd + usage.cost_usd };
      break;
    }
    case "agent.status_changed": agent(event.payload.agent_id, { status: event.payload.status }); break;
    case "agent.stuck_detected": agent(event.payload.agent_id, { status: "stuck" }); break;
    case "agent.error": if (!event.payload.recoverable) agent(event.payload.agent_id, { status: "error" }); break;
    case "agent.stopped": agent(event.payload.agent_id, { status: "stopped", current_task_id: null }); break;
    case "board.entry_posted": next.board = [...next.board.filter(entry => entry.id !== event.payload.entry.id), event.payload.entry]; break;
    case "board.entry_status_changed": next.board = next.board.map(entry => entry.id === event.payload.entry_id ? { ...entry, status: event.payload.status } : entry); break;
    case "artifact.published": next.artifacts = { ...next.artifacts, [event.payload.artifact.id]: event.payload.artifact }; break;
    case "artifact.fetched":
    case "user.steer": break; // Retained in activity for the agent timeline.
    case "user.takeover_started": agent(event.payload.agent_id, { status: "paused_by_user" }); break;
    case "user.takeover_ended": agent(event.payload.agent_id, { status: next.agents[event.payload.agent_id]?.current_task_id ? "working" : "idle" }); break;
    case "approval.requested": next.approvals = { ...next.approvals, [event.payload.approval.id]: event.payload.approval }; break;
    case "approval.decided": next.approvals = { ...next.approvals, [event.payload.approval.id]: event.payload.approval }; break;
    case "supervisor.triggered":
    case "supervisor.action": next.supervisorFeed = [...next.supervisorFeed, event]; break;
    default: return unreachable(event);
  }
  if (next.session && next.status) {
    next.session = { ...next.session, status: next.status, usage: next.usage, ended_at: ["completed", "failed", "stopped"].includes(next.status) ? (next.session.ended_at ?? event.ts) : next.session.ended_at };
  }
  return next;
}

/** Ephemeral heartbeats never advance the persisted sequence cursor. */
export function applyHeartbeat(state: SessionState, heartbeat: Heartbeat): SessionState {
  const agent = state.agents[heartbeat.agent_id];
  if (state.sessionId !== heartbeat.session_id || !agent || (agent.last_heartbeat_at && agent.last_heartbeat_at >= heartbeat.ts)) return state;
  return { ...state, agents: { ...state.agents, [agent.id]: { ...agent, last_heartbeat_at: heartbeat.ts } } };
}
export function createSessionStore(sessionId: string) {
  return createStore<SessionState & {
    receive: (event: Event) => void;
    hydrate: (detail: SessionDetail) => void;
  }>()((set) => ({
    ...initialSessionState(sessionId),
    receive: event => set(state => applyEvent(state, event)),
    hydrate: detail => set(state => {
      if (detail.session.id !== sessionId || detail.last_seq < state.lastSeq) return state;
      return { ...initialSessionState(sessionId), session: detail.session, status: detail.session.status, usage: detail.session.usage, plan: detail.plan, tasks: Object.fromEntries(detail.tasks.map(task => [task.id, task])), agents: Object.fromEntries(detail.agents.map(agent => [agent.id, agent])), lastSeq: detail.last_seq };
    }),
  }));
}
