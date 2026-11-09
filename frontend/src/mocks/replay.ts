import type { Event, Frame } from "@/contracts/generated";
import { agents as baseAgents, eventPayloads, makeEvent } from "./fixtures";

/** Independent stress fixture; never changes the scripted API/socket session. */
export function largeReplayFixture() {
  const sessionId = "replay-stress";
  const agents = ["Ada", "Brook", "Cleo", "Dara", "Eli", "Finn"].map((name, index) => ({ ...baseAgents[0], id: `replay-agent-${index + 1}`, session_id: sessionId, name, color: ["#38bdf8", "#a78bfa", "#34d399", "#fb7185", "#fbbf24", "#2dd4bf"][index] }));
  const events: Event[] = [], frames: Frame[] = [];
  const add = (event: Event, ts: string) => events.push({ ...event, ts });
  const timestamp = (step: number) => new Date(Date.UTC(2026, 0, 1, 12) + step * 1000).toISOString();
  for (const agent of agents) add(makeEvent("agent.created", { agent }, events.length + 1, sessionId), timestamp(0));
  for (const [index, agent] of agents.entries()) add(makeEvent("task.started", { task_id: `task-${index}`, task_key: `T${index}`, agent_id: agent.id, attempt: 1 }, events.length + 1, sessionId), timestamp(0));
  for (let step = 1; step <= 500; step++) {
    for (const [index, agent] of agents.entries()) {
      const frame: Frame = { id: `${agent.id}-frame-${step}`, session_id: sessionId, agent_id: agent.id, step, ts: timestamp(step), task_key: `T${index}`, storage_key_small: `frames/${agent.id}/${step}.jpg`, storage_key_full: null, action_label: `Read source ${step}`, phash: "0123456789abcdef" };
      frames.push(frame);
      add({ ...makeEvent("agent.step", { ...eventPayloads["agent.step"], agent_id: agent.id, task_id: `task-${index}`, task_key: `T${index}`, step, frame, action: { tool: "browser", args: { url: `https://example.test/${step}` }, result: "Read source", is_error: false, latency_ms: 10 } }, events.length + 1, sessionId), actor: { type: "agent", id: agent.id, name: agent.name } }, timestamp(step));
      if (step % 50 === 0) add(makeEvent("board.entry_posted", { entry: { ...eventPayloads["board.entry_posted"].entry, id: `${agent.id}-post-${step}`, session_id: sessionId, seq: events.length + 1, author: { type: "agent", id: agent.id, name: agent.name }, kind: "fact", title: `Source ${step}`, body: "<script>untrusted board text</script>", to: [agents[(index + 1) % agents.length].id], created_at: timestamp(step) } }, events.length + 1, sessionId), timestamp(step));
    }
    if (step === 100 || step === 110) add(makeEvent(step === 100 ? "user.takeover_started" : "user.takeover_ended", { agent_id: agents[0].id }, events.length + 1, sessionId), timestamp(step));
    if (step === 250) {
      add(makeEvent("approval.decided", { approval: { ...eventPayloads["approval.decided"].approval, session_id: sessionId, agent_id: agents[0].id } }, events.length + 1, sessionId), timestamp(step));
      add({ ...makeEvent("supervisor.action", eventPayloads["supervisor.action"], events.length + 1, sessionId), actor: { type: "supervisor", id: null, name: "Supervisor" } }, timestamp(step));
    }
  }
  for (const [index] of agents.entries()) add(makeEvent("task.accepted", { task_id: `task-${index}`, task_key: `T${index}`, note: "Done" }, events.length + 1, sessionId), timestamp(500));
  return { sessionId, agents, events, frames };
}
