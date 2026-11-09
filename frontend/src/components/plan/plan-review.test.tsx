import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { nodeErrors } from "./validation";
import { PlanReview } from "./plan-review";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/env", () => ({ env: { apiUrl: "http://localhost:8000", wsUrl: "ws://localhost:8000", mock: true } }));
// React Flow's geometry and handles are checked separately and in a real browser.
// Keep integration tests focused on the real editor, API, events and draft state.
vi.mock("./plan-graph", async importOriginal => {
  const original = await importOriginal<typeof import("./plan-graph")>();
  return { ...original, PlanGraph: (props: Parameters<typeof original.PlanGraph>[0]) => <div aria-label="Test graph">{props.tasks.map(task => <div key={task.key}><button onClick={() => props.onSelect({ type: "node", key: task.key })}>Select {task.key}</button><span>{task.title}</span><ul aria-label={`Node ${task.key} errors`}>{nodeErrors(task.key, props.errors).map((error, i) => <li key={i}>{error}</li>)}</ul></div>)}<button onClick={() => props.onSelect({ type: "edge", source: "T1", target: "T3" })}>Select T1 to T3 edge</button><button onClick={() => props.onConnect("T3", "T1")}>Connect T3 to T1</button></div> };
});

function renderReview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><PlanReview sessionId="demo-session" /></QueryClientProvider>);
}
async function ready() { renderReview(); await screen.findByRole("button", { name: "Select T1" }); }
async function select(key = "T1") { await userEvent.click(screen.getByRole("button", { name: `Select ${key}` })); }
beforeEach(() => { localStorage.clear(); push.mockClear(); });
afterEach(() => vi.restoreAllMocks());

describe("Plan review", () => {
  it.each(["metaKey", "ctrlKey"])("refines instead of approving on %s+Enter in the refine box", async modifier => {
    const refine = vi.spyOn(api, "refine"); const approve = vi.spyOn(api, "approve"); await ready();
    const input = screen.getByLabelText("Refine with the planner");
    fireEvent.change(input, { target: { value: "Fewer tasks" } });
    fireEvent.keyDown(input, { key: "Enter", [modifier]: true });
    await screen.findByRole("region", { name: "Refinement diff" });
    expect(refine).toHaveBeenCalledTimes(1); expect(approve).not.toHaveBeenCalled();
  });
  it("does not approve from task fields or a deletion dialog", async () => {
    const approve = vi.spyOn(api, "approve"); await ready(); await select();
    for (const label of ["Title", "Goal", "Output 1 kind"]) fireEvent.keyDown(screen.getByLabelText(label), { key: "Enter", metaKey: true });
    await userEvent.click(screen.getByRole("button", { name: "Delete task" }));
    fireEvent.keyDown(screen.getByRole("button", { name: "Cancel deletion" }), { key: "Enter", ctrlKey: true });
    expect(approve).not.toHaveBeenCalled();
  });
  it("uses Backspace to remove selection while ignoring it inside fields", async () => {
    await ready(); await select();
    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Backspace" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Backspace" });
    expect(screen.getByRole("alertdialog")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Cancel deletion" }));
    await userEvent.click(screen.getByRole("button", { name: "Select T1 to T3 edge" }));
    fireEvent.keyDown(window, { key: "Backspace" });
    await select("T3"); expect(screen.getByLabelText("Dependencies (select multiple)")).toHaveValue(["T2"]);
  });
  it("loads planner context and approves the current version before navigating", async () => {
    const approve = vi.spyOn(api, "approve"); await ready();
    expect(screen.getByText("Public sources are sufficient.")).toBeVisible();
    expect(screen.getByText("Which launch market should the report prioritize?")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/sessions/demo-session"));
    expect(approve.mock.calls[0]).toEqual(["demo-session", { plan_version: 1 }]);
  });
  it("updates the draft through the editor, reorders criteria and saves a confirmed plan", async () => {
    await ready(); await select();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Checked launch research" } });
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: "Compare public evidence" } });
    await userEvent.click(screen.getByRole("button", { name: "Add criterion" }));
    fireEvent.change(screen.getByLabelText("Criterion 2"), { target: { value: "Second check" } });
    await userEvent.click(screen.getByRole("button", { name: "Move criterion 2 up" }));
    expect(screen.getByLabelText("Criterion 1")).toHaveValue("Second check");
    fireEvent.change(screen.getByLabelText("New tag"), { target: { value: "sources" } }); await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    expect(within(screen.getByRole("list", { name: "Task tags" })).getByText("sources")).toBeVisible();
    await userEvent.click(screen.getByRole("checkbox", { name: "terminal" }));
    await userEvent.click(screen.getByRole("button", { name: "Save plan" }));
    expect(await screen.findByText("Plan saved.")).toBeVisible();
    const task = (await api.session("demo-session")).plan?.proposal.tasks[0];
    expect(task?.title).toBe("Checked launch research"); expect(task?.goal).toBe("Compare public evidence"); expect(task?.acceptance_criteria[0]).toBe("Second check"); expect(task?.tags).toContain("sources"); expect(task?.suggested_tools).toContain("terminal");
  });
  it("blocks missing and blank criteria and enforces the maximum of six", async () => {
    await ready(); await select(); await userEvent.click(screen.getByRole("button", { name: "Remove criterion 1" }));
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    expect(screen.getAllByText("Add at least one acceptance criterion.").length).toBeGreaterThan(0);
    fireEvent.keyDown(window, { key: "Enter", ctrlKey: true }); expect(push).not.toHaveBeenCalled();
    for (let i = 0; i < 6; i++) await userEvent.click(screen.getByRole("button", { name: "Add criterion" }));
    expect(screen.getByRole("button", { name: "Add criterion" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
  });
  it("edits outputs, validates schema JSON and connects inputs to upstream outputs", async () => {
    await ready(); await select("T3");
    await userEvent.selectOptions(screen.getByLabelText("Output 1 kind"), "json");
    fireEvent.change(screen.getByLabelText("Output 1 JSON schema (optional)"), { target: { value: "{" } });
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid JSON");
    fireEvent.change(screen.getByLabelText("Output 1 JSON schema (optional)"), { target: { value: '{"type":"bogus"}' } });
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Output 1 JSON schema (optional)"), { target: { value: '{"type":"object"}' } });
    fireEvent.change(screen.getByLabelText("Output 1 key"), { target: { value: "summary" } });
    fireEvent.change(screen.getByLabelText("Output 1 description"), { target: { value: "Summary JSON" } });
    await userEvent.click(screen.getByRole("button", { name: "Add input" }));
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Input 3 upstream task"), "T1");
    expect(screen.getByLabelText("Input 3 output key")).toHaveValue("result");
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Save plan" })); await screen.findByText("Plan saved.");
    const task = (await api.session("demo-session")).plan?.proposal.tasks[2];
    expect(task?.outputs[0]).toEqual({ key: "summary", kind: "json", description: "Summary JSON", json_schema: { type: "object" } });
    expect(task?.inputs[2]).toEqual({ from_task: "T1", output_key: "result" });
  });
  it("confirms node deletion, removes dangling edges and ignores Delete inside fields", async () => {
    await ready(); await select();
    fireEvent.keyDown(screen.getByLabelText("Title"), { key: "Delete" }); expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Delete" });
    expect(screen.getByRole("alertdialog")).toHaveTextContent("connected edges");
    await userEvent.click(screen.getByRole("button", { name: "Cancel deletion" })); expect(screen.getByRole("button", { name: "Select T1" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Delete task" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete task" }));
    expect(screen.queryByRole("button", { name: "Select T1" })).not.toBeInTheDocument();
    await select("T3"); expect(screen.getByLabelText("Dependencies (select multiple)")).toHaveValue(["T2"]);
    expect(screen.queryByLabelText("Input 2 upstream task")).not.toBeInTheDocument();
  });
  it("connects a cycle and deletes a selected edge with the keyboard", async () => {
    await ready(); await userEvent.click(screen.getByRole("button", { name: "Connect T3 to T1" }));
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    expect(within(screen.getByRole("list", { name: "Node T3 errors" })).getByText(/Dependency cycle/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Select T1 to T3 edge" }));
    fireEvent.keyDown(window, { key: "Delete" });
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeEnabled();
    await select("T3"); expect(screen.getByLabelText("Dependencies (select multiple)")).toHaveValue(["T2"]);
  });
  it("adds tasks and validates the initially blank goal and criterion", async () => {
    await ready(); await userEvent.click(screen.getByRole("button", { name: "Add task" }));
    expect(screen.getByRole("complementary", { name: "Edit task T4" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: "Check result" } });
    fireEvent.change(screen.getByLabelText("Criterion 1"), { target: { value: "Result is visible" } });
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeEnabled();
  });
  it("shows PUT server errors on the matching node and clears them after correction", async () => {
    const save = vi.spyOn(api, "updatePlan").mockRejectedValueOnce(new ApiError({ type: "about:blank", title: "Invalid plan", status: 422, detail: "Check task T2", instance: null, errors: [{ task_key: "T2", message: "Server rejected T2 output", code: "invalid_json_schema", loc: null, details: null }] }));
    await ready(); await select("T2"); fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Changed" } });
    await userEvent.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Node T2 errors" })).getByText("Server rejected T2 output")).toBeVisible());
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    expect(within(screen.getByRole("list", { name: "Node T1 errors" })).queryByText("Server rejected T2 output")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Corrected" } });
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeEnabled(); expect(save).toHaveBeenCalledTimes(1);
  });
  it("maps structural server errors to the task identified by request location", async () => {
    vi.spyOn(api, "updatePlan").mockRejectedValueOnce(new ApiError({ type: "about:blank", title: "Invalid request", status: 422, detail: null, instance: null, errors: [{ task_key: null, message: "Invalid output schema", code: null, loc: ["body", "tasks", 1, "outputs", 0], details: null }] }));
    await ready(); await userEvent.click(screen.getByRole("button", { name: "Save plan" }));
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Node T2 errors" })).getByText("Invalid output schema")).toBeVisible());
  });
  it("updates dependencies through multi-select and removes detached input wiring", async () => {
    await ready(); await select("T3");
    await userEvent.deselectOptions(screen.getByLabelText("Dependencies (select multiple)"), "T1");
    expect(screen.getByLabelText("Dependencies (select multiple)")).toHaveValue(["T2"]);
    expect(screen.getByLabelText("Input 1 upstream task")).toHaveValue("T2");
    expect(screen.queryByLabelText("Input 2 upstream task")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Dependencies (select multiple)"), "T1");
    expect(screen.getByLabelText("Dependencies (select multiple)")).toHaveValue(["T1", "T2"]);
  });
  it("shows added, removed and changed refinement diff, then accepts it", async () => {
    const refine = vi.spyOn(api, "refine"); await ready();
    fireEvent.change(screen.getByLabelText("Refine with the planner"), { target: { value: "Make it fewer tasks" } });
    await userEvent.click(screen.getByRole("button", { name: "Refine plan" }));
    const diff = await screen.findByRole("region", { name: "Refinement diff" });
    expect(within(diff).getByText(/Added · summary/).parentElement).toHaveClass("bg-green-500/10");
    expect(within(diff).getByText(/Removed · T2/).parentElement).toHaveClass("bg-red-500/10");
    expect(within(diff).getByText("Changed · T1").parentElement).toHaveClass("bg-amber-500/10");
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Accept refinement" }));
    expect(screen.getByRole("button", { name: "Select summary" })).toBeVisible(); expect(screen.queryByRole("button", { name: "Select T2" })).not.toBeInTheDocument();
    expect(refine.mock.calls[0]).toEqual(["demo-session", { instruction: "Make it fewer tasks" }]);
    expect(screen.getByRole("button", { name: "Approve plan" })).toBeEnabled();
  });
  it("discards refinement and restores the reviewed draft before keyboard approval", async () => {
    const save = vi.spyOn(api, "updatePlan"); await ready();
    fireEvent.change(screen.getByLabelText("Refine with the planner"), { target: { value: "Simplify" } });
    await userEvent.click(screen.getByRole("button", { name: "Refine plan" })); await screen.findByRole("region", { name: "Refinement diff" });
    await userEvent.click(screen.getByRole("button", { name: "Discard refinement" }));
    expect(screen.getByRole("button", { name: "Select T2" })).toBeVisible();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true }); fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    await waitFor(() => expect(push).toHaveBeenCalledTimes(1));
    expect(save).toHaveBeenCalledTimes(1); expect(save.mock.calls[0][1].base_version).toBe(2);
    expect((await api.session("demo-session")).plan?.proposal.tasks).toHaveLength(3);
  });
  it("reports refinement errors and enables retry", async () => {
    vi.spyOn(api, "refine").mockRejectedValueOnce(new Error("Planner unavailable")); await ready();
    fireEvent.change(screen.getByLabelText("Refine with the planner"), { target: { value: "Simplify" } });
    await userEvent.click(screen.getByRole("button", { name: "Refine plan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Planner unavailable");
    expect(screen.getByRole("button", { name: "Refine plan" })).toBeEnabled();
  });
});
