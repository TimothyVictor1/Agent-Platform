import { createStore } from "zustand/vanilla";
import type { FieldChange, Plan, PlanDiff, PlanProposal, TaskDraft } from "@/contracts/generated";

/** Ignore object-key order while preserving ordered task, criterion and input arrays. */
export function canonicalStringify(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => {
    if (item !== null && typeof item === "object" && !Array.isArray(item)) {
      return Object.fromEntries(Object.entries(item).sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0));
    }
    return item;
  });
}

export function createDraftStore() {
  return createStore<{
    proposal: PlanProposal | null;
    base: Plan | null;
    conflict: Plan | null;
    schemaText: Record<string, string>;
    schemaErrors: Record<string, string>;
    initialize: (plan: Plan) => void;
    load: (plan: Plan) => void;
    changedOnServer: (plan: Plan) => void;
    replace: (proposal: PlanProposal) => void;
    saved: (plan: Plan) => void;
    update: (key: string, patch: Partial<TaskDraft>) => void;
    add: () => string;
    remove: (key: string) => void;
    connect: (source: string, target: string) => void;
    disconnect: (source: string, target: string) => void;
    setSchema: (key: string, index: number, text: string) => void;
    removeOutput: (key: string, index: number) => void;
  }>()((set, get) => ({
    proposal: null, base: null, conflict: null, schemaText: {}, schemaErrors: {},
    initialize: plan => {
      const { proposal, base, conflict, schemaErrors } = get();
      if (!proposal || !base) { get().load(plan); return; }
      if (plan.version <= base.version || (conflict && plan.version <= conflict.version)) return;
      if (canonicalStringify(proposal.tasks) !== canonicalStringify(base.proposal.tasks) || Object.keys(schemaErrors).length > 0) get().changedOnServer(plan);
      else get().load(plan);
    },
    load: plan => set({ proposal: structuredClone(plan.proposal), base: plan, conflict: null, schemaText: {}, schemaErrors: {} }),
    changedOnServer: plan => set({ conflict: plan }),
    replace: proposal => set({ proposal: structuredClone(proposal), schemaText: {}, schemaErrors: {} }),
    saved: plan => set({ base: plan, conflict: null }),
    update: (key, patch) => set(state => ({ proposal: state.proposal && { ...state.proposal, tasks: state.proposal.tasks.map(t => t.key === key ? { ...t, ...patch } : t) } })),
    add: () => {
      const proposal = get().proposal;
      let index = 1; while (proposal?.tasks.some(t => t.key === `T${index}`)) index++;
      const key = `T${index}`;
      if (proposal) set({ proposal: { ...proposal, tasks: [...proposal.tasks, { key, title: "New task", goal: "", acceptance_criteria: [""], depends_on: [], inputs: [], outputs: [], tags: [], suggested_tools: [], max_attempts: 2 }] } });
      return key;
    },
    remove: key => set(state => ({
      proposal: state.proposal && { ...state.proposal, tasks: state.proposal.tasks.filter(t => t.key !== key).map(t => ({ ...t, depends_on: t.depends_on.filter(d => d !== key), inputs: t.inputs.filter(i => i.from_task !== key) })) },
      schemaText: Object.fromEntries(Object.entries(state.schemaText).filter(([id]) => !id.startsWith(`${key}:`))),
      schemaErrors: Object.fromEntries(Object.entries(state.schemaErrors).filter(([id]) => !id.startsWith(`${key}:`))),
    })),
    connect: (source, target) => {
      const task = get().proposal?.tasks.find(t => t.key === target);
      if (task && !task.depends_on.includes(source)) get().update(target, { depends_on: [...task.depends_on, source] });
    },
    disconnect: (source, target) => {
      const task = get().proposal?.tasks.find(t => t.key === target);
      if (task) get().update(target, { depends_on: task.depends_on.filter(d => d !== source), inputs: task.inputs.filter(i => i.from_task !== source) });
    },
    setSchema: (key, index, text) => {
      const task = get().proposal?.tasks.find(t => t.key === key);
      if (!task) return;
      const id = `${key}:${index}`;
      const schemaErrors = { ...get().schemaErrors }; delete schemaErrors[id];
      let schema: TaskDraft["outputs"][number]["json_schema"] = null;
      try {
        if (text.trim()) {
          const value: unknown = JSON.parse(text);
          if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("Schema must be a JSON object.");
          schema = value as NonNullable<typeof schema>;
        }
      } catch (error) { schemaErrors[id] = error instanceof Error ? error.message : "Invalid JSON."; }
      set(state => ({ schemaText: { ...state.schemaText, [id]: text }, schemaErrors }));
      if (!schemaErrors[id]) get().update(key, { outputs: task.outputs.map((o, i) => i === index ? { ...o, json_schema: schema } : o) });
    },
    removeOutput: (key, index) => {
      const task = get().proposal?.tasks.find(t => t.key === key);
      if (!task) return;
      const shift = (values: Record<string, string>) => Object.fromEntries(Object.entries(values).flatMap(([id, value]) => {
        if (!id.startsWith(`${key}:`)) return [[id, value]];
        const position = Number(id.slice(key.length + 1));
        return position === index ? [] : [[`${key}:${position > index ? position - 1 : position}`, value]];
      }));
      set(state => ({ schemaText: shift(state.schemaText), schemaErrors: shift(state.schemaErrors) }));
      get().update(key, { outputs: task.outputs.filter((_, i) => i !== index) });
    },
  }));
}

export function diffPlans(before: PlanProposal, after: PlanProposal): PlanDiff {
  const old = new Map(before.tasks.map(t => [t.key, t]));
  return {
    added: after.tasks.filter(t => !old.has(t.key)),
    removed: before.tasks.filter(t => !after.tasks.some(next => next.key === t.key)).map(t => t.key),
    changed: after.tasks.flatMap(task => {
      const previous = old.get(task.key); if (!previous) return [];
      const fields = (Object.keys(task) as (keyof TaskDraft)[]).flatMap(field => canonicalStringify(previous[field]) === canonicalStringify(task[field]) ? [] : [{ field, before: previous[field], after: task[field] } satisfies FieldChange]);
      return fields.length ? [{ key: task.key, fields }] : [];
    }),
  };
}
