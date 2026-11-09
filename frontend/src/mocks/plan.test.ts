import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { openMockSocket } from "./socket";
import { isReviewSession, reviewDetail } from "./plan";

vi.mock("@/lib/env", () => ({ env: { apiUrl: "http://localhost:8000", mock: true } }));
beforeEach(() => localStorage.clear());

describe("plan review mock transport", () => {
  it("keeps the scripted 200-event stream for a non-review session after loading it", async () => {
    const session = await api.createSession({ prompt: "Live and replay fixture", model_profile: "local" });
    expect(isReviewSession(session.id)).toBe(false);
    // Even a stale review record must not replace this session's scripted stream.
    reviewDetail(session);
    const detail = await api.session(session.id);
    expect(detail.last_seq).toBe(3);
    expect(detail.plan?.proposal.tasks).toHaveLength(1);
    await api.refine(session.id, { instruction: "Keep the scripted fixture" });
    expect((await api.events(session.id)).items).toHaveLength(200);
    vi.useFakeTimers();
    const receive = vi.fn(); const stop = openMockSocket(session.id, 0, receive);
    try {
      await vi.advanceTimersByTimeAsync(60000);
      expect(receive.mock.calls.filter(([message]) => message.type === "event")).toHaveLength(200);
      expect(receive.mock.calls.at(-1)?.[0]).toMatchObject({ type: "event", event: { type: "session.completed", seq: 200 } });
    } finally { stop(); vi.useRealTimers(); }
  });
  it("persists saved tasks and rejects stale versions and invalid inputs", async () => {
    const detail = await api.session("demo-session");
    const tasks = structuredClone(detail.plan!.proposal.tasks); tasks[0].title = "Edited research";
    await api.updatePlan("demo-session", { tasks, base_version: 1 });
    expect((await api.session("demo-session")).plan).toMatchObject({ version: 2, proposal: { tasks } });
    await expect(api.updatePlan("demo-session", { tasks, base_version: 1 })).rejects.toMatchObject({ status: 409 });
    tasks[0].inputs = [{ from_task: "unknown", output_key: "result" }];
    await expect(api.updatePlan("demo-session", { tasks, base_version: 2 })).rejects.toMatchObject({ status: 422, problem: { errors: [expect.objectContaining({ code: "unknown_input_task", task_key: "T1" })] } });
  });
  it("waits for approval and streams refinement and approval events in order", async () => {
    const detail = await api.session("demo-session");
    vi.useFakeTimers();
    const receive = vi.fn(); const stop = openMockSocket("demo-session", detail.last_seq, receive);
    try {
      await vi.advanceTimersByTimeAsync(500); expect(receive).not.toHaveBeenCalled();
      await api.refine("demo-session", { instruction: "Make it fewer tasks" });
      await vi.advanceTimersByTimeAsync(100);
      expect(receive.mock.calls[0][0]).toMatchObject({ type: "event", event: { type: "session.plan_proposed", seq: 4, payload: { plan_version: 2 } } });
      expect((await api.session("demo-session")).plan?.proposal.tasks).toHaveLength(2);
      await expect(api.approve("demo-session", { plan_version: 1 })).rejects.toMatchObject({ status: 409 });
      await api.approve("demo-session", { plan_version: 2 });
      await vi.advanceTimersByTimeAsync(100);
      expect(receive.mock.calls[1][0]).toMatchObject({ type: "event", event: { type: "session.approved", seq: 5 } });
      const approved = await api.session("demo-session");
      expect(approved.session.status).toBe("running"); expect(approved.plan?.approved_at).toBeTruthy();
    } finally { stop(); vi.useRealTimers(); }
  });
});
