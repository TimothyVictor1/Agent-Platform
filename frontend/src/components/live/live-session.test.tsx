import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Event, SessionDetail, WsThumbnailMessage } from "@/contracts/generated";
import { api } from "@/lib/api";
import { SessionSocket, type ConnectionState } from "@/lib/ws";
import { agents, approval, artifact, boardEntry, eventPayloads, makeEvent, scriptedEvents, session, task } from "@/mocks/fixtures";
import { LiveSession } from "./live-session";
import { StepTimeline } from "./agent-views";

vi.setConfig({ testTimeout: 30000 });
configure({ asyncUtilTimeout: 10000 });

const transport = vi.hoisted(() => ({
  event: (event: Event) => { void event; },
  thumbnail: (thumbnail: WsThumbnailMessage) => { void thumbnail; },
  state: (state: ConnectionState) => { void state; },
  connect: vi.fn(), disconnect: vi.fn(),
}));
vi.mock("@/lib/ws", () => ({ SessionSocket: vi.fn(class {
  connect = transport.connect;
  disconnect = transport.disconnect;
  onEvent(listener: (event: Event) => void) { transport.event = listener; return vi.fn(); }
  onThumbnail(listener: (thumbnail: WsThumbnailMessage) => void) { transport.thumbnail = listener; return vi.fn(); }
  onState(listener: (state: ConnectionState) => void) { transport.state = listener; listener("connected"); return vi.fn(); }
}) }));
vi.mock("@/lib/api", async original => {
  const apiModule = await original<typeof import("@/lib/api")>();
  return { ...apiModule, api: Object.fromEntries(Object.keys(apiModule.api).map(key => [key, vi.fn()])) };
});
vi.mock("@/lib/env", () => ({ env: { mock: false } }));

const detail: SessionDetail = {
  session: { ...session, status: "running", started_at: "2026-01-01T00:00:00Z" }, plan: null,
  agents: agents.map((agent, index) => ({ ...agent, status: "working", current_task_id: task.id, stream_url: `https://desktop.example/${index}/vnc.html`, interactive_stream_url: `https://desktop.example/${index}/vnc.html?control=1` })),
  tasks: [{ ...task, status: "running" }], counts: { total: 1, by_status: { running: 1 } }, last_seq: 0,
};
function mount(sessionId = session.id) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return { client, ...render(<QueryClientProvider client={client}><LiveSession sessionId={sessionId} /></QueryClientProvider>) };
}
async function ready() { const view = mount(); await screen.findByRole("heading", { name: session.title }); await waitFor(() => expect(transport.connect).toHaveBeenCalled()); return view; }
function emit(event: Event) { act(() => transport.event(event)); }

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.session).mockResolvedValue(detail);
  vi.mocked(api.events).mockResolvedValue({ items: [], next_after_seq: null });
  const accepted = { command_id: "command-1", session_id: session.id };
  for (const method of [api.pause, api.resume, api.stop, api.takeover, api.release, api.steer, api.postBoard, api.decideApproval]) vi.mocked(method).mockResolvedValue(accepted);
  vi.mocked(api.frame).mockResolvedValue(new Blob(["frame"], { type: "image/jpeg" }));
  vi.mocked(api.downloadArtifact).mockResolvedValue(new Blob(["report"]));
  vi.stubGlobal("Notification", class {
    static permission = "default";
    static requestPermission = vi.fn().mockResolvedValue("granted");
  });
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = vi.fn().mockReturnValue("blob:test-frame");
    static revokeObjectURL = vi.fn();
  });
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("LiveSession", () => {
  it("renders three agents, budgets, elapsed time and exact sandbox/clipboard restrictions", async () => {
    await ready();
    expect(screen.getAllByRole("article", { name: / agent$/ })).toHaveLength(3);
    expect(screen.getByRole("progressbar", { name: "Step budget used" })).toBeVisible();
    expect(screen.getByLabelText("Elapsed time")).toHaveTextContent(/\d\d:\d\d:\d\d/);
    const frame = screen.getByTitle("Scout view-only desktop");
    expect(frame).toHaveAttribute("sandbox", "allow-scripts allow-same-origin");
    expect(frame).toHaveAttribute("allow", "clipboard-read 'none'; clipboard-write 'none'");
    expect(frame.getAttribute("src")).toContain("view_only=1");
    expect(frame.getAttribute("src")).toContain("show_clipboard=0");
    expect(Notification.requestPermission).not.toHaveBeenCalled();
  });

  it("hydrates REST history and refetches without replacing the socket", async () => {
    vi.mocked(api.session).mockResolvedValueOnce({ ...detail, last_seq: 3 }).mockResolvedValueOnce({ ...detail, last_seq: 4, session: { ...detail.session, title: "Refetched title" } });
    const initial = scriptedEvents().slice(0, 3);
    const posted = makeEvent("board.entry_posted", { entry: boardEntry }, 4);
    vi.mocked(api.events).mockResolvedValueOnce({ items: initial, next_after_seq: null }).mockResolvedValueOnce({ items: [...initial, posted], next_after_seq: null });
    const view = await ready();
    expect(transport.connect).toHaveBeenCalledWith(session.id, 3);
    await act(async () => { await view.client.refetchQueries({ queryKey: ["live-session", session.id] }); });
    expect(await screen.findByRole("heading", { name: "Refetched title" })).toBeVisible();
    expect(await screen.findByText(boardEntry.title)).toBeVisible();
    expect(SessionSocket).toHaveBeenCalledTimes(1);
    expect(transport.connect).toHaveBeenCalledTimes(1);
    expect(transport.disconnect).not.toHaveBeenCalled();
    view.unmount();
    expect(transport.disconnect).toHaveBeenCalledTimes(1);
  });

  it("opens the correct steer recipient and posts trimmed agent and broadcast payloads", async () => {
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Message Builder" }));
    expect(screen.getByLabelText("Message recipient")).toHaveValue("agent-2");
    expect(screen.getByLabelText("Steering message")).toHaveFocus();
    await userEvent.type(screen.getByLabelText("Steering message"), "  Check the sources  ");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.steer).toHaveBeenCalledWith(session.id, { agent_id: "agent-2", text: "Check the sources" }));
    await userEvent.selectOptions(screen.getByLabelText("Message recipient"), "");
    await userEvent.type(screen.getByLabelText("Steering message"), "Continue");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(api.steer).toHaveBeenLastCalledWith(session.id, { agent_id: null, text: "Continue" }));
  });

  it("requires Stop confirmation, supports cancellation and uses reducer status for Pause/Resume", async () => {
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(api.pause).toHaveBeenCalledWith(session.id));
    emit(makeEvent("session.paused", eventPayloads["session.paused"], 1));
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(api.resume).toHaveBeenCalledWith(session.id));
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(screen.getByRole("alertdialog")).toHaveAccessibleName("Stop this session?");
    expect(api.stop).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    expect(api.stop).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm stop" }));
    await waitFor(() => expect(api.stop).toHaveBeenCalledWith(session.id));
  });

  it("uses events to enter and leave takeover and swaps to the interactive URL", async () => {
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Take over Scout" }));
    await waitFor(() => expect(api.takeover).toHaveBeenCalledWith("agent-1"));
    expect(screen.queryByText("You are controlling Scout's desktop")).not.toBeInTheDocument();
    emit(makeEvent("user.takeover_started", { agent_id: "agent-1" }, 1));
    expect(screen.getByText("You are controlling Scout's desktop")).toBeVisible();
    expect(screen.getByTitle("Scout interactive desktop").getAttribute("src")).toContain("control=1");
    expect(screen.getByTitle("Scout interactive desktop").getAttribute("src")).toContain("view_only=0");
    await userEvent.click(screen.getByRole("button", { name: "Return control" }));
    await waitFor(() => expect(api.release).toHaveBeenCalledWith("agent-1"));
    emit(makeEvent("user.takeover_ended", { agent_id: "agent-1" }, 2));
    expect(screen.queryByText("You are controlling Scout's desktop")).not.toBeInTheDocument();
    expect(screen.getByTitle("Scout view-only desktop")).toBeVisible();
  });

  it("filters board text by kind, author, tag and search without interpreting HTML", async () => {
    await ready();
    const unsafe = '<img src=x onerror="alert(1)"><script>evil()</script>';
    emit(makeEvent("board.entry_posted", { entry: { ...boardEntry, title: "Launch finding", kind: "fact", body: unsafe, tags: ["launch"], author: { type: "agent", id: "agent-1", name: "Scout" } } }, 1));
    emit(makeEvent("board.entry_posted", { entry: { ...boardEntry, id: "entry-2", title: "Build update", kind: "progress", tags: ["build"], author: { type: "agent", id: "agent-2", name: "Builder" } } }, 2));
    expect(screen.getByText(unsafe)).toBeVisible();
    expect(screen.getByRole("list", { name: "Board entries" }).querySelector("img,script")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Board kind"), "fact");
    expect(screen.queryByText("Build update")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Board agent"), "agent-2");
    expect(screen.queryByText("Launch finding")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Board agent"), "agent-1");
    await userEvent.selectOptions(screen.getByLabelText("Board tag"), "launch");
    await userEvent.type(screen.getByLabelText("Search board"), "missing");
    expect(screen.getByText("No board entries match.")).toBeVisible();
    await userEvent.clear(screen.getByLabelText("Search board"));
    await userEvent.type(screen.getByLabelText("Search board"), "LAUNCH");
    expect(screen.getByText("Launch finding")).toBeVisible();
  });

  it("composes a user decision to one agent", async () => {
    await ready();
    await userEvent.selectOptions(screen.getByLabelText("Post kind"), "decision");
    await userEvent.selectOptions(screen.getByLabelText("Post recipient"), "agent-3");
    await userEvent.type(screen.getByLabelText("Post title"), "  Use public sources  ");
    await userEvent.type(screen.getByLabelText("Post text"), "  Cite each finding  ");
    await userEvent.click(screen.getByRole("button", { name: "Post to board" }));
    await waitFor(() => expect(api.postBoard).toHaveBeenCalledWith(session.id, { kind: "decision", title: "Use public sources", body: "Cite each finding", to: ["agent-3"] }));
  });

  it("shows approval frames, badges and decisions, then removes decided cards", async () => {
    await ready();
    emit(makeEvent("approval.requested", { approval: { ...approval, frame_id: "frame-1" } }, 1));
    await userEvent.click(screen.getByRole("tab", { name: /Approvals/ }));
    expect(screen.getByLabelText("1 pending approvals")).toBeVisible();
    expect(screen.getByText("Scout · Risk: medium")).toBeVisible();
    expect(await screen.findByAltText(`Approval screenshot for ${approval.action_summary}`)).toBeVisible();
    await waitFor(() => expect(api.frame).toHaveBeenCalledWith("frame-1"));
    await userEvent.type(screen.getByLabelText("Decision note"), "Use the draft instead");
    await userEvent.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(api.decideApproval).toHaveBeenCalledWith(approval.id, { decision: "deny", note: "Use the draft instead" }));
    emit(makeEvent("approval.decided", { approval: { ...approval, status: "denied" } }, 2));
    expect(screen.queryByRole("article", { name: `Approval: ${approval.action_summary}` })).not.toBeInTheDocument();
    expect(screen.getByLabelText("0 pending approvals")).toBeVisible();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:test-frame");
  });

  it("requests notification permission only on click and notifies for new approvals once", async () => {
    const notify = vi.fn();
    const requestPermission = vi.fn().mockResolvedValue("granted");
    vi.stubGlobal("Notification", Object.assign(class { constructor(title: string, options: NotificationOptions) { notify(title, options); } }, { permission: "granted", requestPermission }));
    await ready();
    expect(requestPermission).not.toHaveBeenCalled();
    const requested = makeEvent("approval.requested", { approval }, 1);
    emit(requested); emit(requested);
    expect(notify).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenCalledWith("Approval requested", { body: approval.action_summary, tag: approval.id });
    await userEvent.click(screen.getByRole("tab", { name: /Approvals/ }));
    await userEvent.click(screen.getByRole("button", { name: "Enable approval notifications" }));
    expect(requestPermission).toHaveBeenCalledTimes(1);
  });

  it("shows a focused timeline, board activity and the latest thumbnail on stream error", async () => {
    await ready();
    emit(makeEvent("agent.step", { ...eventPayloads["agent.step"], reasoning_excerpt: "Compare public sources" }, 1));
    emit(makeEvent("board.entry_posted", { entry: { ...boardEntry, author: { type: "agent", id: "agent-1", name: "Scout" } } }, 2));
    act(() => transport.thumbnail({ type: "thumbnail", agent_id: "agent-1", jpeg_b64: "abc", step: 1 }));
    await userEvent.click(screen.getByRole("button", { name: "Focus Scout" }));
    expect(screen.getByRole("region", { name: "Scout focused view" })).toBeVisible();
    expect(screen.getByText("Compare public sources")).toBeVisible();
    expect(within(screen.getByRole("region", { name: "Scout focused view" })).getByText(boardEntry.title)).toBeVisible();
    expect(screen.getByLabelText("Message recipient")).toHaveValue("agent-1");
    fireEvent.error(screen.getByTitle("Scout view-only desktop"));
    expect(screen.getByAltText("Scout's latest desktop thumbnail, step 1")).toHaveAttribute("src", "data:image/jpeg;base64,abc");
  });

  it("shows DAG dependencies, result outputs, artifact downloads and supervisor reasons", async () => {
    await ready();
    emit(makeEvent("task.created", { task: { ...task, id: "task-2", key: "report", title: "Write report", depends_on: [task.key], status: "pending" } }, 1));
    emit(makeEvent("task.result_submitted", { ...eventPayloads["task.result_submitted"], result: { ...eventPayloads["task.result_submitted"].result, outputs: { source_count: 3 } } }, 2));
    emit(makeEvent("artifact.published", { artifact }, 3));
    emit(makeEvent("supervisor.action", eventPayloads["supervisor.action"], 4));
    await userEvent.click(screen.getByRole("tab", { name: "Tasks" }));
    expect(screen.getByLabelText("Task dependency graph").querySelectorAll("polyline")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: `Task ${task.key}: ${task.title}` }));
    const details = screen.getByRole("region", { name: "Task details" });
    expect(within(details).getByText(/"source_count": 3/)).toBeVisible();
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    await userEvent.click(within(details).getByRole("button", { name: "Download report.txt" }));
    await waitFor(() => expect(api.downloadArtifact).toHaveBeenCalledWith(artifact.id));
    expect(click).toHaveBeenCalled(); click.mockRestore();
    await userEvent.click(screen.getByRole("tab", { name: "Supervisor" }));
    expect(screen.getByText("Why: All criteria met")).toBeVisible();
  });

  it("shows reconnect progress and completion links driven by events", async () => {
    await ready();
    act(() => transport.state("reconnecting"));
    expect(screen.getByText(/Reconnecting automatically/)).toHaveTextContent("Resume from event 0");
    emit(makeEvent("session.completed", { summary: "Done" }, 1));
    expect(screen.getByRole("link", { name: "View Replay" })).toHaveAttribute("href", `/sessions/${session.id}/replay`);
    expect(screen.getByRole("link", { name: "View artifacts" })).toHaveAttribute("href", "#session-artifacts");
    expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  });

  it("cleans up and opens an isolated socket when the session id changes", async () => {
    const view = await ready();
    vi.mocked(api.session).mockResolvedValue({ ...detail, session: { ...detail.session, id: "other", title: "Other session" }, agents: [], tasks: [] });
    view.rerender(<QueryClientProvider client={view.client}><LiveSession sessionId="other" /></QueryClientProvider>);
    await screen.findByRole("heading", { name: "Other session" });
    await waitFor(() => expect(transport.connect).toHaveBeenLastCalledWith("other", 0));
    expect(transport.disconnect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("article", { name: "Scout agent" })).not.toBeInTheDocument();
  });

  it("opens the session panel as an accessible bottom sheet", async () => {
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "Open session panel" }));
    const sheet = screen.getByRole("dialog", { name: "Session panel" });
    expect(within(sheet).getByRole("tab", { name: "Board" })).toBeVisible();
    await userEvent.click(within(sheet).getByRole("tab", { name: /Approvals/ }));
    expect(within(sheet).getByText("No pending approvals.")).toBeVisible();
    await userEvent.click(within(sheet).getByRole("button", { name: "Close panel" }));
    expect(screen.queryByRole("dialog", { name: "Session panel" })).not.toBeInTheDocument();
  });

  it("retries an initial REST failure before connecting the socket", async () => {
    vi.mocked(api.session).mockRejectedValueOnce(new Error("Session offline"));
    mount();
    expect(await screen.findByRole("alert")).toHaveTextContent("Session offline");
    expect(transport.connect).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByRole("heading", { name: session.title });
    await waitFor(() => expect(transport.connect).toHaveBeenCalledTimes(1));
  });
});

it("virtualizes the step timeline while keeping later actions reachable", () => {
  const steps = Array.from({ length: 100 }, (_, i) => makeEvent("agent.step", { ...eventPayloads["agent.step"], step: i + 1, reasoning_excerpt: `Reason ${i + 1}` }, i + 1));
  render(<StepTimeline steps={steps} />);
  expect(screen.getAllByRole("listitem").length).toBeLessThan(12);
  expect(screen.queryByText("Reason 100")).not.toBeInTheDocument();
  fireEvent.scroll(screen.getByRole("list", { name: "Step timeline" }), { target: { scrollTop: 97 * 128 } });
  expect(screen.getByText("Reason 100")).toBeVisible();
});
