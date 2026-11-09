import { describe, expect, it } from "vitest";
import type { Plan, TaskDraft } from "@/contracts/generated";
import { canonicalStringify, createDraftStore, diffPlans } from "./draft-store";
import { cycleEdges, fieldErrors, nodeErrors, schemaError, validatePlan } from "./validation";

export const draftTask = (key: string, patch: Partial<TaskDraft> = {}): TaskDraft => ({ key, title: `Task ${key}`, goal: "Produce a checked result", acceptance_criteria: ["Result is visible"], depends_on: [], inputs: [], outputs: [], tags: [], suggested_tools: [], max_attempts: 2, ...patch });
const plan = (tasks: TaskDraft[]): Plan => ({ session_id: "demo-session", version: 1, created_at: "2026-01-01T12:00:00Z", approved_at: null, proposal: { tasks, rationale: "", assumptions: [], questions_for_user: [] } });

describe("plan validation", () => {
  it("reports a cycle path and highlights every participating node and directed edge", () => {
    const tasks = [draftTask("A", { depends_on: ["B"] }), draftTask("B", { depends_on: ["C"] }), draftTask("C", { depends_on: ["A"] }), draftTask("D")];
    const errors = validatePlan(tasks);
    expect(errors).toHaveLength(1); expect(errors[0].cycle).toEqual(["A", "B", "C", "A"]);
    expect(cycleEdges(errors)).toEqual(new Set(["B->A", "C->B", "A->C"]));
    expect(nodeErrors("B", errors)).toHaveLength(1); expect(nodeErrors("D", errors)).toEqual([]);
  });
  it.each([[], ["", "  "], Array<string>(7).fill("Check")].map(acceptance_criteria => ({ acceptance_criteria })))("rejects invalid criteria $acceptance_criteria", ({ acceptance_criteria }) => {
    expect(validatePlan([draftTask("A", { acceptance_criteria })]).length).toBeGreaterThan(0);
  });
  it("checks all cross-task input rules", () => {
    const errors = validatePlan([draftTask("A"), draftTask("B", { inputs: [{ from_task: "missing", output_key: "data" }, { from_task: "A", output_key: "data" }] })]);
    expect(errors.map(e => e.code)).toEqual(["unknown_input_task", "input_not_dependency", "unknown_input_output"]);
  });
  it("checks size, duplicate keys, dependencies and outputs", () => {
    expect(validatePlan([])[0].code).toBe("empty_plan");
    expect(validatePlan(Array.from({ length: 13 }, (_, i) => draftTask(`T${i}`)))[0].code).toBe("too_many_tasks");
    expect(validatePlan([draftTask("A", { depends_on: ["A", "absent"] }), draftTask("A")]).map(e => e.code)).toEqual(["duplicate_key", "self_dependency", "unknown_dependency"]);
    expect(validatePlan([draftTask("A", { outputs: [{ key: "x", kind: "text", description: "", json_schema: {} }, { key: "x", kind: "json", description: "", json_schema: { type: "bogus" } }] })]).map(e => e.code)).toEqual(["json_schema_on_non_json", "duplicate_output_key", "invalid_json_schema"]);
  });
  it("validates nested schemas and form field limits without evaluating references", () => {
    expect(schemaError({ type: "object", properties: { list: { type: "array", items: { $ref: "https://untrusted.invalid/schema" } } } })).toBeNull();
    expect(schemaError({ properties: { count: { minimum: "zero" } } })).toContain("minimum");
    expect(schemaError({ required: ["x", "x"] })).toContain("required");
    expect(fieldErrors([draftTask("A", { title: "", goal: "" })])).toHaveLength(2);
  });
});

describe("draft state", () => {
  it("canonicalizes nested object keys while keeping array order significant", () => {
    expect(canonicalStringify({ b: { d: 1, c: 2 }, a: [1, 2] })).toBe(canonicalStringify({ a: [1, 2], b: { c: 2, d: 1 } }));
    expect(canonicalStringify([1, 2])).not.toBe(canonicalStringify([2, 1]));
  });
  it("adopts newer clean plans and preserves invalid schema text as an unsaved edit", () => {
    const source = plan([draftTask("A", { outputs: [{ key: "x", kind: "json", description: "", json_schema: null }] })]);
    const store = createDraftStore(); store.getState().initialize(source);
    const newer = { ...source, version: 2, proposal: { ...source.proposal, rationale: "New rationale" } };
    store.getState().initialize(newer);
    expect(store.getState().base?.version).toBe(2); expect(store.getState().proposal?.rationale).toBe("New rationale");
    store.getState().setSchema("A", 0, "{");
    store.getState().initialize({ ...newer, version: 3 });
    expect(store.getState().schemaText["A:0"]).toBe("{"); expect(store.getState().conflict?.version).toBe(3);
  });
  it("updates the store while keeping the server snapshot untouched", () => {
    const source = plan([draftTask("A")]); const store = createDraftStore(); store.getState().initialize(source);
    store.getState().update("A", { title: "Edited", acceptance_criteria: ["Second", "First"] });
    expect(store.getState().proposal?.tasks[0].title).toBe("Edited"); expect(source.proposal.tasks[0].title).toBe("Task A");
    store.getState().initialize(plan([draftTask("A", { title: "Incoming" })]));
    expect(store.getState().proposal?.tasks[0].title).toBe("Edited");
  });
  it("adds, connects, disconnects and removes tasks with dangling edges and input refs", () => {
    const store = createDraftStore(); store.getState().initialize(plan([draftTask("A"), draftTask("B")]));
    store.getState().connect("A", "B"); store.getState().connect("A", "B");
    expect(store.getState().proposal?.tasks[1].depends_on).toEqual(["A"]);
    store.getState().update("B", { inputs: [{ from_task: "A", output_key: "x" }] });
    store.getState().disconnect("A", "B"); expect(store.getState().proposal?.tasks[1].inputs).toEqual([]);
    store.getState().connect("A", "B"); store.getState().remove("A");
    expect(store.getState().proposal?.tasks[0].depends_on).toEqual([]);
    expect(store.getState().add()).toBe("T1"); expect(store.getState().add()).toBe("T2");
  });
  it("keeps invalid JSON text, blocks it, and clears errors when the output is removed", () => {
    const store = createDraftStore(); store.getState().initialize(plan([draftTask("A", { outputs: [{ key: "x", kind: "json", description: "", json_schema: null }] })]));
    store.getState().setSchema("A", 0, "{");
    expect(store.getState().schemaErrors["A:0"]).toBeTruthy(); expect(store.getState().schemaText["A:0"]).toBe("{");
    store.getState().setSchema("A", 0, '{"type":"object"}');
    expect(store.getState().schemaErrors).toEqual({}); expect(store.getState().proposal?.tasks[0].outputs[0].json_schema).toEqual({ type: "object" });
    store.getState().setSchema("A", 0, "{"); store.getState().removeOutput("A", 0);
    expect(store.getState().schemaErrors).toEqual({});
  });
  it("diffs added, removed and changed tasks by stable key", () => {
    const before = plan([draftTask("A"), draftTask("B")]).proposal;
    const after = plan([draftTask("A", { title: "Edited" }), draftTask("C")]).proposal;
    expect(diffPlans(before, after)).toEqual({ added: [after.tasks[1]], removed: ["B"], changed: [{ key: "A", fields: [{ field: "title", before: "Task A", after: "Edited" }] }] });
  });
});
