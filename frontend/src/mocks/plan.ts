import type { Event, PlanProposal, Session, SessionDetail, TaskDraft } from "@/contracts/generated";
import { applyEvent, initialSessionState } from "@/lib/session-store";
import { makeEvent, proposal } from "./fixtures";

const task = (key: string, title: string, patch: Partial<TaskDraft> = {}): TaskDraft => ({ key, title, goal: title, acceptance_criteria: ["Result includes sources and can be checked from the output."], depends_on: [], inputs: [], outputs: [{ key: "result", kind: "text", description: "Checked findings with sources", json_schema: null }], tags: ["launch"], suggested_tools: ["browser"], max_attempts: 2, ...patch });

export const planFixture: PlanProposal = {
  rationale: "Research options in parallel, then compare the evidence in a final report.",
  assumptions: ["Public sources are sufficient.", "The report should compare three launch options."],
  questions_for_user: ["Which launch market should the report prioritize?"],
  tasks: [task("T1", "Research launch options"), task("T2", "Gather pricing evidence", { outputs: [{ key: "prices", kind: "json", description: "Prices with source links", json_schema: { type: "array", items: { type: "object" } } }] }), task("T3", "Write the launch comparison", { depends_on: ["T1", "T2"], inputs: [{ from_task: "T1", output_key: "result" }, { from_task: "T2", output_key: "prices" }], suggested_tools: ["office", "files"], outputs: [{ key: "report", kind: "artifact", description: "Launch comparison report", json_schema: null }] })],
};

const storageKey = (sessionId: string) => `agentplatform.mock.plan.${sessionId}`;
export function reviewEvents(sessionId: string): Event[] | null {
  if (typeof localStorage === "undefined") return null;
  const raw = localStorage.getItem(storageKey(sessionId));
  if (!raw) return null;
  try { return JSON.parse(raw) as Event[]; } catch { return null; }
}
function persist(sessionId: string, events: Event[]) { if (typeof localStorage !== "undefined") localStorage.setItem(storageKey(sessionId), JSON.stringify(events)); }

export function reviewDetail(current: Session): SessionDetail {
  let events = reviewEvents(current.id);
  if (!events) {
    events = [makeEvent("session.created", { session: current }, 1, current.id), makeEvent("session.planning_started", { reason: "initial", instruction: null }, 2, current.id), makeEvent("session.plan_proposed", { plan_version: 1, proposal: current.id === "demo-session" ? planFixture : proposal, diff: null }, 3, current.id)];
    persist(current.id, events);
  }
  const state = events.reduce(applyEvent, initialSessionState(current.id));
  return { session: state.session ?? current, plan: state.plan, tasks: Object.values(state.tasks), agents: Object.values(state.agents), counts: { total: Object.keys(state.tasks).length, by_status: {} }, last_seq: state.lastSeq };
}

export function reviewCommand(current: Session, action: "plan" | "refine" | "approve", tasks?: TaskDraft[], instruction?: string) {
  const detail = reviewDetail(current);
  const events = reviewEvents(current.id) ?? [];
  const version = detail.plan?.version ?? 1;
  if (action === "approve") events.push(makeEvent("session.approved", { plan_version: version }, events.length + 1, current.id));
  else {
    const original = detail.plan?.proposal ?? planFixture;
    const next: PlanProposal = action === "plan" ? { ...original, tasks: tasks ?? original.tasks } : refinedProposal(original, instruction ?? "");
    if (action === "refine") events.push(makeEvent("session.plan_proposed", { plan_version: version + 1, proposal: next, diff: null }, events.length + 1, current.id));
    else events.push(makeEvent("session.plan_updated", { plan_version: version + 1, proposal: next, source: "user", reason: null }, events.length + 1, current.id));
  }
  persist(current.id, events);
}

/** Deterministic fixture, with add/remove/change examples and a fewer-tasks variant. */
export function refinedProposal(original: PlanProposal, instruction: string): PlanProposal {
  const first = original.tasks[0] ?? planFixture.tasks[0];
  const fewer = /fewer|less|simpl|merge/i.test(instruction);
  const key = original.tasks.some(t => t.key === "summary") ? "summary2" : "summary";
  return { ...original, rationale: `Mock refinement: ${instruction}`, tasks: [
    { ...first, title: "Research and consolidate launch evidence", depends_on: [], inputs: [], outputs: [{ key: "evidence", kind: "text", description: "Consolidated evidence", json_schema: null }] },
    ...(!fewer || original.tasks.length > 2 ? [task(key, "Prepare the final launch summary", { depends_on: [first.key], inputs: [{ from_task: first.key, output_key: "evidence" }], suggested_tools: ["office"] })] : []),
  ] };
}
