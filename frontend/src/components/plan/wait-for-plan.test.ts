import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSessionStore } from "@/lib/session-store";
import { makeEvent, session } from "@/mocks/fixtures";
import { planFixture } from "@/mocks/plan";
import { waitForPlan } from "./plan-review";

const update = (version: number, causationId: string | null) => ({
  ...makeEvent("session.plan_updated", { plan_version: version, proposal: planFixture, source: "user", reason: null }, version, session.id),
  causation_id: causationId,
});
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("plan save confirmation", () => {
  it("ignores another command's newer plan, including a refresh of that version, until the matching update arrives", async () => {
    const store = createSessionStore(session.id);
    const confirmed = vi.fn();
    const saved = waitForPlan(store, plan => plan.version > 1, "save-command").then(confirmed);
    store.getState().receive(update(2, "another-command"));
    const plan = store.getState().plan;
    store.getState().hydrate({ session, plan, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 2 });
    await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled();
    store.getState().receive(update(3, "save-command"));
    await saved; expect(confirmed).toHaveBeenCalledWith(expect.objectContaining({ version: 3 }));
  });
  it("does not confirm a planner proposal even if it shares the command id", async () => {
    const store = createSessionStore(session.id);
    const confirmed = vi.fn();
    const saved = waitForPlan(store, plan => plan.version > 1, "save-command").then(confirmed);
    store.getState().receive({ ...makeEvent("session.plan_proposed", { plan_version: 2, proposal: planFixture, diff: null }, 2), causation_id: "save-command" });
    await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled();
    store.getState().receive(update(3, "save-command")); await saved;
    expect(confirmed).toHaveBeenCalledWith(expect.objectContaining({ version: 3 }));
  });
  it.each([null, "missing"])("keeps the version fallback when causation is %s", async causation => {
    const store = createSessionStore(session.id);
    const confirmed = vi.fn();
    const saved = waitForPlan(store, plan => plan.version > 1, "save-command").then(confirmed);
    const old = update(1, null); const newer = update(2, null);
    if (causation === "missing") { Reflect.deleteProperty(old, "causation_id"); Reflect.deleteProperty(newer, "causation_id"); }
    store.getState().receive(old);
    await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled();
    store.getState().receive(newer); await saved;
    expect(confirmed).toHaveBeenCalledWith(expect.objectContaining({ version: 2 }));
  });
  it("recognizes an update that arrived before the PUT response", async () => {
    const store = createSessionStore(session.id);
    store.getState().receive(update(2, "save-command"));
    await expect(waitForPlan(store, plan => plan.version > 1, "save-command")).resolves.toMatchObject({ version: 2 });
    expect(vi.getTimerCount()).toBe(0);
  });
  it("ignores an unrelated update already present when the PUT response arrives", async () => {
    const store = createSessionStore(session.id);
    store.getState().receive(update(2, "another-command"));
    const confirmed = vi.fn();
    const saved = waitForPlan(store, plan => plan.version > 1, "save-command").then(confirmed);
    await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled();
    store.getState().receive(update(3, "save-command")); await saved;
    expect(confirmed).toHaveBeenCalledWith(expect.objectContaining({ version: 3 }));
  });
  it("preserves version confirmation from a REST snapshot with no event metadata", async () => {
    const store = createSessionStore(session.id);
    const saved = waitForPlan(store, plan => plan.version > 1, "save-command");
    store.getState().hydrate({ session, plan: { session_id: session.id, version: 2, proposal: planFixture, created_at: session.created_at, approved_at: null }, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 2 });
    await expect(saved).resolves.toMatchObject({ version: 2 });
  });
});
