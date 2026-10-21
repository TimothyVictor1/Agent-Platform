import { describe, expect, it } from "vitest";
import type { Event, TaskAssignedEvent } from "./generated";

// Compile-time checks that the generated union narrows on `type`.
function agentOf(event: Event): string | null {
  if (event.type === "task.assigned") {
    const assigned: TaskAssignedEvent = event;
    return assigned.payload.agent_id;
  }
  return null;
}

describe("generated contracts", () => {
  it("narrow the Event union by type", () => {
    const event: Event = {
      id: "e1",
      session_id: "s1",
      seq: 1,
      ts: "2026-01-01T00:00:00Z",
      actor: { type: "system", id: null, name: "system" },
      causation_id: null,
      correlation_id: "t1",
      type: "task.assigned",
      payload: {
        task_id: "t1",
        task_key: "T1",
        agent_id: "a1",
        attempt: 1,
        ack_deadline: "2026-01-01T00:00:10Z",
        lease_expires_at: "2026-01-01T00:00:45Z",
      },
    };
    expect(agentOf(event)).toBe("a1");
  });
});
