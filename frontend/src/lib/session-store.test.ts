import { readFileSync } from "node:fs";
import ts from "typescript";
import { describe, expect, it } from "vitest";
import type { Event } from "@/contracts/generated";
import { agents, allEventFixtures, approval, artifact, boardEntry, eventPayloads, makeEvent, scriptedEvents, session, task } from "@/mocks/fixtures";
import { applyEvent, applyHeartbeat, createSessionStore, initialSessionState } from "./session-store";

const seed = () => ({ ...initialSessionState(session.id), session, status: session.status, tasks: { [task.id]: task }, agents: Object.fromEntries(agents.map(agent => [agent.id, agent])), approvals: { [approval.id]: approval }, artifacts: { [artifact.id]: artifact }, board: [boardEntry] });
function apply(type: Event["type"]) { return applyEvent(seed(), makeEvent(type, eventPayloads[type], 1)); }

describe("applyEvent", () => {
  it("advances the cursor for unknown event types without changing any other state", () => {
    const state = seed();
    const event = { ...makeEvent("user.steer", eventPayloads["user.steer"], 6), type: "future.event" } as unknown as Event;
    const next = applyEvent(state, event);
    expect(next).toEqual({ ...state, lastSeq: 6 });
    expect(next.activity).toBe(state.activity);
    expect(next.session).toBe(state.session);
  });
  it("keeps the latest 500 activity events", () => {
    const events = Array.from({ length: 501 }, (_, i) => makeEvent("user.steer", eventPayloads["user.steer"], i + 1));
    const state = events.reduce(applyEvent, initialSessionState(session.id));
    expect(state.activity).toHaveLength(500);
    expect(state.activity[0].seq).toBe(2);
    expect(state.activity.at(-1)?.seq).toBe(501);
  });
  it.each(["running", "paused"] as const)("preserves %s status when a plan changes", status => {
    for (const type of ["session.plan_proposed", "session.plan_updated"] as const) {
      const state = { ...seed(), status, session: { ...session, status } };
      const next = applyEvent(state, makeEvent(type, eventPayloads[type], 1));
      expect(next.status).toBe(status);
      expect(next.session?.status).toBe(status);
      expect(next.plan?.version).toBe(eventPayloads[type].plan_version);
    }
  });
  it("preserves collections missing from REST when hydrating at the current sequence", () => {
    const store = createSessionStore(session.id);
    store.getState().receive(makeEvent("approval.requested", { approval }, 1));
    store.getState().receive(makeEvent("artifact.published", { artifact }, 2));
    store.getState().receive(makeEvent("supervisor.action", eventPayloads["supervisor.action"], 3));
    store.getState().receive(makeEvent("board.entry_posted", { entry: { ...boardEntry, seq: 5 } }, 5));
    const before = store.getState();
    store.getState().hydrate({ session, plan: null, tasks: [task], agents, counts: { total: 1, by_status: { pending: 1 } }, last_seq: 5 });
    const after = store.getState();
    expect(after.board).toEqual([{ ...boardEntry, seq: 5 }]);
    expect(after.board).toBe(before.board);
    expect(after.approvals).toBe(before.approvals);
    expect(after.artifacts).toBe(before.artifacts);
    expect(after.supervisorFeed).toBe(before.supervisorFeed);
    expect(after.activity).toBe(before.activity);
    expect(after.tasks[task.id]).toEqual(task);
    expect(after.agents[agents[0].id]).toEqual(agents[0]);
    expect(after.lastSeq).toBe(5);
  });

  it("handles every event in the generated union, including future contract additions", () => {
    // Inspect generated.ts directly: unlike Vitest type assertions this runs without tsc.
    const source = ts.createSourceFile("generated.ts", readFileSync("src/contracts/generated.ts", "utf8"), ts.ScriptTarget.Latest, true);
    const aliases = source.statements.filter(ts.isTypeAliasDeclaration);
    const union = aliases.find(node => node.name.text === "Event");
    if (!union || !ts.isUnionTypeNode(union.type)) throw new Error("Generated Event union not found");
    const interfaces = source.statements.filter(ts.isInterfaceDeclaration);
    const names = union.type.types.map(node => node.getText(source));
    const contractTypes = names.map(name => {
      const node = interfaces.find(node => node.name.text === name);
      const field = node?.members.find(member => ts.isPropertySignature(member) && member.name.getText(source) === "type");
      if (!field || !ts.isPropertySignature(field) || !field.type || !ts.isLiteralTypeNode(field.type) || !ts.isStringLiteral(field.type.literal)) throw new Error(`Missing discriminator for ${name}`);
      return field.type.literal.text;
    });
    expect(Object.keys(eventPayloads).sort()).toEqual(contractTypes.sort());
    for (const event of allEventFixtures) {
      const state = seed();
      const next = applyEvent(state, event);
      expect(next.lastSeq, event.type).toBe(event.seq);
      expect(next.activity.at(-1)?.type, event.type).toBe(event.type);
      expect(state.lastSeq).toBe(0);
    }

  });
  it("projects session lifecycle, plan versions and budget notifications", () => {
    expect(apply("session.plan_proposed").plan?.version).toBe(1);
    expect(apply("session.plan_updated").session?.plan_version).toBe(2);
    expect(apply("session.approved").status).toBe("running");
    expect(apply("session.paused").status).toBe("paused");
    expect(apply("session.resumed").status).toBe("running");
    for (const type of ["session.failed", "session.planning_failed"] as const) expect(apply(type).status).toBe("failed");
    expect(apply("session.stopped").session?.ended_at).not.toBeNull();
    expect(apply("session.budget_exceeded").status).toBe("planning");
  });
  it("projects task changes, results, retries and terminal states", () => {
    const statuses = { "task.ready": "ready", "task.assigned": "assigned", "task.assignment_acked": "pending", "task.started": "running", "task.result_submitted": "in_review", "task.review_started": "in_review", "task.accepted": "done", "task.rework_requested": "ready", "task.failed": "ready", "task.cancelled": "cancelled" } as const;
    for (const [type, status] of Object.entries(statuses)) expect(apply(type as keyof typeof statuses).tasks[task.id].status, type).toBe(status);
    expect(apply("task.assigned").tasks[task.id].assigned_agent_id).toBe(agents[0].id);
    expect(apply("task.result_submitted").tasks[task.id].result?.summary).toContain("three");
    expect(apply("task.rework_requested").tasks[task.id].rework_count).toBe(1);
    expect(apply("task.updated").tasks[task.id].title).toBe("Updated research");
    const failure = makeEvent("task.failed", { ...eventPayloads["task.failed"], will_retry: false }, 1);
    expect(applyEvent(seed(), failure).tasks[task.id].status).toBe("failed");
  });
  it("projects agents, takeover, board, approvals, artifacts and supervisor feed", () => {
    expect(apply("agent.sandbox_ready").agents[agents[0].id].stream_url).toContain("6080");
    expect(apply("agent.status_changed").agents[agents[0].id].status).toBe("working");
    expect(apply("agent.stuck_detected").agents[agents[0].id].status).toBe("stuck");
    expect(apply("agent.error").agents[agents[0].id].status).toBe("error");
    expect(apply("agent.stopped").agents[agents[0].id].status).toBe("stopped");
    expect(apply("user.takeover_started").agents[agents[0].id].status).toBe("paused_by_user");
    expect(apply("user.takeover_ended").agents[agents[0].id].status).toBe("idle");
    expect(apply("board.entry_posted").board).toHaveLength(1);
    expect(apply("board.entry_status_changed").board[0].status).toBe("resolved");
    expect(apply("approval.decided").approvals[approval.id].status).toBe("approved");
    expect(apply("artifact.published").artifacts[artifact.id]).toEqual(artifact);
    expect(apply("supervisor.action").supervisorFeed).toHaveLength(1);
  });
  it("is idempotent across duplicate, older, wrong-session events and reconnect replay", () => {
    const events = scriptedEvents();
    expect(events).toHaveLength(200);
    const first = events.slice(0, 100).reduce(applyEvent, initialSessionState());
    const resumed = events.slice(80).reduce(applyEvent, first);
    const uninterrupted = events.reduce(applyEvent, initialSessionState());
    expect(resumed).toEqual(uninterrupted);
    expect(resumed.status).toBe("completed");
    expect(resumed.usage.steps).toBe(180);
    expect(Object.keys(resumed.agents)).toHaveLength(3);
    expect(applyEvent(resumed, events[10])).toBe(resumed);
    expect(applyEvent(resumed, { ...events[0], session_id: "other", seq: 201 })).toBe(resumed);
  });
  it("does not count ephemeral heartbeats as persisted events", () => {
    const state = seed();
    const next = applyHeartbeat(state, { type: "agent.heartbeat", session_id: session.id, agent_id: agents[0].id, task_id: task.id, step: 5, ts: "2026-02-01T00:00:00Z" });
    expect(next.lastSeq).toBe(0);
    expect(next.agents[agents[0].id].last_heartbeat_at).toContain("2026-02");
  });
  it("creates isolated stores and rejects stale REST hydration", () => {
    const store = createSessionStore(session.id); const other = createSessionStore("other");
    store.getState().receive(makeEvent("session.created", { session }, 5));
    store.getState().hydrate({ session, plan: null, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 4 });
    expect(store.getState().lastSeq).toBe(5);
    expect(other.getState().lastSeq).toBe(0);
    expect(typeof store.getState().receive).toBe("function");
  });
});
