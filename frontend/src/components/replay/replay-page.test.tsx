import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { allEventFixtures, eventPayloads, makeEvent, session } from "@/mocks/fixtures";
import { largeReplayFixture } from "@/mocks/replay";
import { EventDetails } from "./event-details";
import { ReplayPage } from "./replay-page";

vi.mock("@/lib/env", () => ({ env: { apiUrl: "http://localhost:8000", mock: true } }));
beforeEach(async () => {
  localStorage.setItem("agentplatform.mock.sessions", JSON.stringify([{ ...session, id: "scripted-replay" }]));
  await api.session("scripted-replay");
}, 30000);
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}><ReplayPage sessionId="scripted-replay" /></QueryClientProvider>);
}

describe("replay page", () => {
  it("loads the existing mock through api.ts and supports playback, navigation, filters and typing guards", async () => {
    const events = vi.spyOn(api, "events"); const frame = vi.spyOn(api, "frame");
    renderPage();
    await screen.findByRole("button", { name: "Play" });
    expect(screen.getByText(/200 events/)).toBeVisible();
    expect(events).toHaveBeenCalledWith("scripted-replay", { after_seq: 0, limit: 500 });
    expect(frame).not.toHaveBeenCalled();
    expect(screen.getByText("No saved frame at this moment.")).toBeVisible();
    expect(screen.getAllByLabelText(/lane$/)).toHaveLength(4);
    fireEvent.keyDown(window, { key: "ArrowRight" });
    expect(within(screen.getByLabelText("Event details")).getByText("session.planning_started")).toBeVisible();
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    expect(within(screen.getByLabelText("Event details")).getByText("session.created")).toBeVisible();
    await userEvent.selectOptions(screen.getByLabelText("Event type"), "board.entry_posted");
    await userEvent.click(screen.getByRole("button", { name: "Next event" }));
    expect(within(screen.getByLabelText("Event details")).getByText("Comparing launch options.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Next event" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("Speed"), "16");
    fireEvent.keyDown(screen.getByLabelText("Speed"), { key: " " });
    fireEvent.keyDown(screen.getByLabelText("Playhead"), { key: "ArrowRight" });
    expect(screen.getByRole("button", { name: "Play" })).toBeVisible();
    const before = screen.getByLabelText("Playback time").textContent;
    for (const tag of ["input", "textarea", "div"]) {
      const input = document.createElement(tag);
      if (tag === "div") input.setAttribute("contenteditable", "true");
      document.body.append(input); fireEvent.keyDown(input, { key: " " }); fireEvent.keyDown(input, { key: "ArrowRight" }); input.remove();
    }
    expect(screen.getByRole("button", { name: "Play" })).toBeVisible();
    expect(screen.getByLabelText("Playback time").textContent).toBe(before);
    fireEvent.keyDown(window, { key: " " });
    expect(screen.getByRole("button", { name: "Pause" })).toBeVisible();
    await waitFor(() => expect(screen.getByLabelText("Playback time").textContent).not.toBe(before));
    fireEvent.keyDown(window, { key: " " });
    expect(screen.getByRole("button", { name: "Play" })).toBeVisible();
  }, 30000);
  it("shows each agent's latest frame at the playhead and changes the focused viewer", async () => {
    const fixture = largeReplayFixture();
    const detail = await api.session("scripted-replay");
    vi.spyOn(api, "session").mockResolvedValue({ ...detail, agents: fixture.agents });
    vi.spyOn(api, "events").mockResolvedValue({ items: fixture.events.filter(event => event.type === "agent.created" || event.type === "agent.step" && event.payload.step <= 3), next_after_seq: null });
    vi.spyOn(api, "frames").mockResolvedValue({ items: fixture.frames.filter(frame => frame.step <= 3) });
    vi.spyOn(api, "frame").mockResolvedValue(new Blob(["jpeg"], { type: "image/jpeg" }));
    vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn(() => "blob:frame"); static revokeObjectURL = vi.fn(); });
    renderPage(); await screen.findByRole("button", { name: "Play" });
    expect(screen.getByText("No saved frame at this moment.")).toBeVisible();
    fireEvent.change(screen.getByLabelText("Playhead"), { target: { value: "1500" } });
    expect(await screen.findByAltText("Ada at step 1: Read source 1")).toBeVisible();
    const strip = screen.getByLabelText("All agents at playhead");
    await waitFor(() => expect(within(strip).getAllByRole("img")).toHaveLength(6));
    await userEvent.selectOptions(screen.getByLabelText("Selected agent"), fixture.agents[1].id);
    expect(await screen.findByAltText("Brook at step 1: Read source 1")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Ada, step 2: Read source 2" }));
    expect(await screen.findByAltText("Ada at step 2: Read source 2")).toBeVisible();
    expect(within(screen.getByLabelText("Event details")).getByText("Read the next source")).toBeVisible();
  }, 30000);
  it("renders board text, tool calls, reasoning and approvals as plain text", () => {
    const text = "<img src=x onerror=alert(1)>";
    const event = makeEvent("board.entry_posted", { entry: { ...eventPayloads["board.entry_posted"].entry, title: text, body: "<script>untrusted</script>" } }, 1);
    const view = render(<EventDetails event={event} />);
    expect(screen.getByText("<script>untrusted</script>")).toBeVisible();
    expect(view.container.querySelector("img, script")).toBeNull();
    view.rerender(<EventDetails event={makeEvent("agent.step", { ...eventPayloads["agent.step"], reasoning_excerpt: text, action: { tool: "browser", args: { text }, result: "<b>done</b>", is_error: false, latency_ms: 0 } }, 2)} />);
    expect(screen.getByText(text)).toBeVisible();
    expect(screen.getAllByText(/"tool": "browser"/)[0]).toHaveTextContent("<b>done</b>");
    expect(view.container.querySelector("img, b, script")).toBeNull();
    view.rerender(<EventDetails event={allEventFixtures.find(event => event.type === "approval.decided")!} />);
    expect(screen.getByText("approved")).toBeVisible();
  });
  it("shows load failures and retries the page", async () => {
    vi.spyOn(api, "frames").mockRejectedValueOnce(new Error("Frame index offline"));
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Frame index offline");
    await userEvent.click(screen.getByRole("button", { name: "Retry replay" }));
    expect(await screen.findByRole("button", { name: "Play" })).toBeVisible();
  });
  it("handles an empty log without starting playback", async () => {
    vi.spyOn(api, "events").mockResolvedValue({ items: [], next_after_seq: null });
    renderPage();
    await screen.findByText("No events have been recorded for this session.");
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();
    fireEvent.keyDown(window, { key: " " });
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  });
  it("downloads JSON and a valid empty mock ZIP", async () => {
    const blobs: Blob[] = [];
    vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn((blob: Blob) => { blobs.push(blob); return "blob:download"; }); static revokeObjectURL = vi.fn(); });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    renderPage(); await screen.findByRole("button", { name: "Play" });
    await userEvent.click(screen.getByRole("button", { name: "Download events JSON" }));
    expect(blobs[0].type).toBe("application/json");
    await userEvent.click(screen.getByRole("button", { name: "Download artifacts ZIP" }));
    await waitFor(() => expect(blobs).toHaveLength(2));
    expect(blobs[1].type).toBe("application/zip"); expect(blobs[1].size).toBe(22);
    expect(click).toHaveBeenCalledTimes(2);
    await act(async () => {});
  });
});
