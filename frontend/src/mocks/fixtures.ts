import type { Agent, Approval, Artifact, BoardEntry, Event, PlanProposal, ProfileList, ProvidersHealth, Session, Task } from "@/contracts/generated";
import { emptyUsage } from "@/lib/session-store";

export const timestamp = "2026-01-01T12:00:00Z";
export const actor = { type: "system", id: null, name: "Platform" } as const;
export const budgets: Session["budgets"] = { max_steps_per_task: 150, max_total_steps: 1500, max_wall_minutes: 60, max_cost_usd: null, max_tokens: null };
export const session: Session = { id: "demo-session", user_id: "local", title: "Research the next launch", prompt: "Research the next launch and prepare a report", status: "planning", agent_count: 3, model_profile: "local", sandbox_provider: "docker_local", mode: "coordinated", plan_version: 0, budgets, usage: emptyUsage(), created_at: timestamp, started_at: null, ended_at: null };
export const agents: Agent[] = ["Scout", "Builder", "Reviewer"].map((name, i) => ({ id: `agent-${i + 1}`, session_id: session.id, name, color: ["#38bdf8", "#a78bfa", "#34d399"][i], status: "booting", sandbox: null, current_task_id: null, step: 0, stream_url: null, interactive_stream_url: null, last_heartbeat_at: null }));
export const task: Task = { id: "task-1", session_id: session.id, key: "research", title: "Research launch options", goal: "Compare launch options", acceptance_criteria: ["Three options with sources"], depends_on: [], inputs: [], outputs: [], tags: [], suggested_tools: ["browser"], max_attempts: 3, plan_version: 1, status: "pending", assigned_agent_id: null, attempt: 0, rework_count: 0, result: null, created_by: "planner" };
export const proposal: PlanProposal = { tasks: [{ key: task.key, title: task.title, goal: task.goal, acceptance_criteria: task.acceptance_criteria, depends_on: [], inputs: [], outputs: [], tags: [], suggested_tools: ["browser"], max_attempts: 3 }], rationale: "Research, build, then review.", assumptions: ["Public sources are sufficient"], questions_for_user: [] };
export const boardEntry: BoardEntry = { id: "entry-1", session_id: session.id, seq: 1, author: actor, kind: "note", title: "Research started", body: "Comparing launch options.", tags: [], task_key: task.key, refs: [], to: null, artifact_id: null, status: "open", created_at: timestamp };
export const artifact: Artifact = { id: "artifact-1", session_id: session.id, name: "report.txt", mime: "text/plain", size_bytes: 12, sha256: "0".repeat(64), produced_by: actor, task_key: task.key, source_path: null, storage_key: "demo/report.txt", version: 1, description: "Launch report", created_at: timestamp };
export const approval: Approval = { id: "approval-1", session_id: session.id, agent_id: agents[0].id, task_key: task.key, action_summary: "Publish launch report", risk: "medium", frame_id: null, status: "pending", decided_by: null, note: null, created_at: timestamp, decided_at: null, expires_at: null };
export const profiles: ProfileList = { default_profile: "local", items: [{ name: "local", roles: { worker: { provider: "openai_compatible", model: "demo-worker", base_url: "http://localhost:8080", vision: true, coordinate_mode: "absolute" } } }] };
export const health: ProvidersHealth = { items: ["local-model", "docker_local", "e2b"].map((name, i) => ({ name, kind: i === 0 ? "model" : "sandbox", ok: i !== 2, latency_ms: i === 2 ? null : 12, error: i === 2 ? "Not configured" : null, checked_at: timestamp })) };
const ref = { task_id: task.id, task_key: task.key };
const result = { summary: "Compared three launch options", outputs: {}, artifact_ids: [artifact.id], confidence: "high", open_issues: [], final_frame_id: null } as const;

/** A payload for every generated persisted event. New contracts require a fixture here. */
export const eventPayloads = {
  "session.created": { session },
  "session.planning_started": { reason: "initial", instruction: null },
  "session.plan_proposed": { plan_version: 1, proposal, diff: null },
  "session.planning_failed": { reason: "No valid plan", errors: [] },
  "session.plan_updated": { plan_version: 2, proposal, source: "user", reason: null },
  "session.approved": { plan_version: 2 },
  "session.paused": { reason: "User paused" },
  "session.resumed": {},
  "session.completed": { summary: "Done" },
  "session.failed": { reason: "Demo error" },
  "session.stopped": { reason: "user", detail: null },
  "session.budget_warning": { metric: "total_steps", used: 80, limit: 100 },
  "session.budget_exceeded": { metric: "total_steps", used: 100, limit: 100 },
  "task.created": { task },
  "task.updated": { task: { ...task, title: "Updated research" }, changed_fields: ["title"] },
  "task.ready": ref,
  "task.assigned": { ...ref, agent_id: agents[0].id, attempt: 1, ack_deadline: timestamp, lease_expires_at: timestamp },
  "task.assignment_acked": { ...ref, agent_id: agents[0].id, attempt: 1 },
  "task.started": { ...ref, agent_id: agents[0].id, attempt: 1 },
  "task.result_submitted": { ...ref, agent_id: agents[0].id, result: { ...result, artifact_ids: [artifact.id], open_issues: [] } },
  "task.review_started": ref,
  "task.accepted": { ...ref, note: null },
  "task.rework_requested": { ...ref, agent_id: agents[0].id, instructions: "Add sources", rework_count: 1 },
  "task.failed": { ...ref, reason: "Timeout", attempt: 1, will_retry: true, agent_id: agents[0].id },
  "task.cancelled": { ...ref, reason: "Cancelled by user" },
  "agent.created": { agent: agents[0] },
  "agent.sandbox_booting": { agent_id: agents[0].id, provider: "docker_local" },
  "agent.sandbox_ready": { agent_id: agents[0].id, sandbox: { provider: "docker_local", sandbox_id: "sandbox-1", created_at: timestamp, expires_at: null }, stream_url: "http://localhost:6080", interactive_stream_url: "http://localhost:6080" },
  "agent.step": { ...ref, agent_id: agents[0].id, step: 1, coordination_calls: [], action: null, reasoning_excerpt: "Read the next source", frame: null, usage: { steps: 1, input_tokens: 100, output_tokens: 20, images: 1, cost_usd: 0.001 }, model_latency_ms: 100 },
  "agent.status_changed": { agent_id: agents[0].id, status: "working", previous: "idle", reason: null },
  "agent.stuck_detected": { agent_id: agents[0].id, task_key: task.key, reason: "no_progress", detail: "No progress", frame_ids: [] },
  "agent.error": { agent_id: agents[0].id, error: "Timeout", recoverable: false },
  "agent.stopped": { agent_id: agents[0].id, reason: "Completed" },
  "board.entry_posted": { entry: boardEntry },
  "board.entry_status_changed": { entry_id: boardEntry.id, status: "resolved", previous: "open", reason: null },
  "artifact.published": { artifact },
  "artifact.fetched": { artifact_id: artifact.id, agent_id: agents[1].id, sandbox_path: "/workspace/report.txt" },
  "user.steer": { agent_id: null, text: "Focus on public sources", entry_id: null },
  "user.takeover_started": { agent_id: agents[0].id },
  "user.takeover_ended": { agent_id: agents[0].id },
  "approval.requested": { approval },
  "approval.decided": { approval: { ...approval, status: "approved", decided_by: actor, decided_at: timestamp } },
  "supervisor.triggered": { reason: "result_submitted", detail: "Review launch report", task_key: task.key, agent_id: agents[0].id, entry_ids: [] },
  "supervisor.action": { tool: "accept_task", args: {}, why: "All criteria met", result: "Accepted", trigger_event_id: null },
} satisfies { [K in Event["type"]]: Extract<Event, { type: K }>["payload"] };
type PayloadMap = { [K in Event["type"]]: Extract<Event, { type: K }>["payload"] };
export function makeEvent<K extends Event["type"]>(type: K, payload: PayloadMap[K], seq: number, sessionId = session.id): Extract<Event, { type: K }> {
  // The generic couples discriminator and payload; TS cannot distribute an object over a union.
  return { id: `${sessionId}-event-${seq}`, session_id: sessionId, seq, ts: new Date(Date.parse(timestamp) + seq * 300).toISOString(), actor, causation_id: null, correlation_id: null, type, payload } as Extract<Event, { type: K }>;
}
export const allEventFixtures: Event[] = (Object.keys(eventPayloads) as Event["type"][]).map((type, i) => makeEvent(type, eventPayloads[type], i + 1));
/** Deterministic 200-event session, emitted over 60 seconds by the mock socket. */
export function scriptedEvents(sessionId = session.id): Event[] {
  const events: Event[] = [];
  const add = <K extends Event["type"]>(type: K, payload: PayloadMap[K]) => events.push(makeEvent<K>(type, payload, events.length + 1, sessionId));
  add("session.created", { session: { ...session, id: sessionId } });
  add("session.planning_started", eventPayloads["session.planning_started"]);
  add("session.plan_proposed", eventPayloads["session.plan_proposed"]);
  add("session.approved", { plan_version: 1 });
  for (const agent of agents) {
    add("agent.created", { agent: { ...agent, session_id: sessionId } });
    add("agent.sandbox_ready", { ...eventPayloads["agent.sandbox_ready"], agent_id: agent.id });
  }
  add("task.created", { task: { ...task, session_id: sessionId } });
  add("task.assigned", eventPayloads["task.assigned"]);
  add("task.started", eventPayloads["task.started"]);
  add("board.entry_posted", { entry: { ...boardEntry, session_id: sessionId } });
  while (events.length < 194) {
    const i = events.length - 14;
    add("agent.step", { ...eventPayloads["agent.step"], agent_id: agents[i % 3].id, step: Math.floor(i / 3) + 1 });
  }
  add("task.result_submitted", eventPayloads["task.result_submitted"]);
  add("artifact.published", { artifact: { ...artifact, session_id: sessionId } });
  add("supervisor.triggered", eventPayloads["supervisor.triggered"]);
  add("supervisor.action", eventPayloads["supervisor.action"]);
  add("task.accepted", eventPayloads["task.accepted"]);
  add("session.completed", eventPayloads["session.completed"]);
  return events;
}
