import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { SessionSocket } from "@/lib/ws";
vi.mock("@/lib/env", () => ({ env: { apiUrl: "http://localhost:8000", wsUrl: "ws://localhost:8000", mock: true } }));
beforeEach(() => localStorage.clear());
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("mock mode", () => {
  it("serves fresh tickets with the expected lifetime and validates the session", async () => {
    const first = await api.wsTicket("demo-session");
    const second = await api.wsTicket("demo-session");
    expect(first.ticket).toMatch(/^[A-Za-z0-9_-]{32,128}$/);
    expect(first.ttl_s).toBe(60);
    expect(Date.parse(first.expires_at) - Date.now()).toBeCloseTo(60000, -2);
    expect(second.ticket).not.toBe(first.ticket);
    await expect(api.wsTicket("missing")).rejects.toMatchObject({ status: 404 });
  });
  it("creates, lists and loads sessions without fetching the backend", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const session = await api.createSession({ prompt: "Prepare report", model_profile: "local", agent_count: 2, sandbox_provider: "e2b" });
    expect(session.status).toBe("planning"); expect(session.agent_count).toBe(2);
    expect((await api.sessions()).items[0].id).toBe(session.id);
    const detail = await api.session(session.id); expect(detail.session.prompt).toBe("Prepare report"); expect(detail.plan?.proposal.tasks).toHaveLength(1);
    expect((await api.providerHealth()).items).toHaveLength(3); expect((await api.profiles()).items[0].name).toBe("local");
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("paginates fixture events, returns binary data and reports missing sessions", async () => {
    const page = await api.events("demo-session", { after_seq: 10, limit: 5 });
    expect(page.items).toHaveLength(5); expect(page.items[0].seq).toBe(11); expect(page.next_after_seq).toBe(15);
    expect((await api.frame("frame-1")).type).toBe("image/jpeg");
    await expect(api.session("missing")).rejects.toMatchObject({ status: 404 });
  });
  it("streams 200 ordered events and thumbnails over 60 seconds and resumes at since", async () => {
    const tickets = vi.spyOn(api, "wsTicket");
    // Preload to keep dynamic import latency outside fake-time assertions.
    await import("./socket"); vi.useFakeTimers();
    const events = vi.fn(); const thumbnails = vi.fn(); const socket = new SessionSocket();
    socket.onEvent(events); socket.onThumbnail(thumbnails); socket.connect("demo-session");
    await vi.waitFor(() => expect(socket.state).toBe("connected"));
    await vi.advanceTimersByTimeAsync(60000);
    expect(events).toHaveBeenCalledTimes(200); expect(events.mock.calls.at(-1)?.[0].type).toBe("session.completed"); expect(thumbnails).toHaveBeenCalledTimes(180);
    socket.disconnect(); socket.connect("demo-session", 190);
    await vi.waitFor(() => expect(socket.state).toBe("connected")); await vi.advanceTimersByTimeAsync(3000);
    expect(tickets).toHaveBeenCalledTimes(2); expect(tickets).toHaveBeenNthCalledWith(2, "demo-session");
    expect(events).toHaveBeenCalledTimes(210); expect(socket.lastSeq).toBe(200); socket.disconnect();
    await vi.advanceTimersByTimeAsync(60000); expect(events).toHaveBeenCalledTimes(210);
  });
});
