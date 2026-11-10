import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { eventPayloads, makeEvent, session } from "@/mocks/fixtures";
import type { WsTicket } from "@/contracts/generated";
import { api, ApiError } from "./api";
import { SessionSocket } from "./ws";
vi.mock("./env", () => ({ env: { wsUrl: "ws://api.example", apiToken: "test-token", mock: false } }));
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  constructor(public url: string) { FakeSocket.instances.push(this); }
  close(code = 1000) { this.onclose?.({ code }); }
  send(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) }); }
}
const ticket = (value = "fake_ws_ticket_for_session_00000001"): WsTicket => ({ ticket: value, expires_at: "2026-11-10T12:01:00Z", ttl_s: 60 }); // gitleaks:allow
beforeEach(() => {
  vi.useFakeTimers(); FakeSocket.instances = []; vi.stubGlobal("WebSocket", FakeSocket);
  vi.spyOn(api, "wsTicket").mockResolvedValue(ticket());
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("SessionSocket", () => {
  it("waits for a ticket before connecting and includes only the ticket and cursor in the URL", async () => {
    const pending = Promise.withResolvers<WsTicket>();
    vi.mocked(api.wsTicket).mockReturnValueOnce(pending.promise);
    const socket = new SessionSocket(); socket.connect("a/b", 12);
    expect(api.wsTicket).toHaveBeenCalledWith("a/b");
    expect(socket.state).toBe("connecting"); expect(FakeSocket.instances).toHaveLength(0);
    pending.resolve(ticket()); await vi.advanceTimersByTimeAsync(0);
    const url = new URL(FakeSocket.instances[0].url);
    expect(url.pathname).toBe("/api/v1/ws/sessions/a%2Fb");
    expect([...url.searchParams]).toEqual([["ticket", ticket().ticket], ["since", "12"]]);
    expect(url.toString()).not.toContain("test-token");
    socket.disconnect();
  });
  it("fetches a fresh ticket after 4401 with the normal backoff and latest cursor", async () => {
    const fresh = ticket("fake_ws_ticket_for_session_00000002"); // gitleaks:allow
    vi.mocked(api.wsTicket).mockResolvedValueOnce(ticket()).mockResolvedValueOnce(fresh);
    const socket = new SessionSocket(); socket.connect(session.id, 3);
    await vi.advanceTimersByTimeAsync(0);
    FakeSocket.instances[0].send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 4) });
    FakeSocket.instances[0].close(4401);
    expect(socket.state).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(999); expect(api.wsTicket).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(api.wsTicket).toHaveBeenCalledTimes(2);
    expect(new URL(FakeSocket.instances[1].url).searchParams.get("ticket")).toBe(fresh.ticket);
    expect(new URL(FakeSocket.instances[1].url).searchParams.get("since")).toBe("4");
    FakeSocket.instances[1].close(4401);
    await vi.advanceTimersByTimeAsync(1999); expect(api.wsTicket).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1); expect(api.wsTicket).toHaveBeenCalledTimes(3);
    socket.disconnect();
  });
  it.each([401, 403])("stops with a clear error when ticket authorization returns %s", async status => {
    const rejected = new ApiError({ type: "about:blank", title: "Denied", status, detail: null, instance: null, errors: null });
    vi.mocked(api.wsTicket).mockRejectedValueOnce(rejected);
    const socket = new SessionSocket(); const errors = vi.fn(); const states = vi.fn();
    socket.onError(errors); socket.onState(states); socket.connect(session.id);
    await vi.advanceTimersByTimeAsync(0);
    expect(socket.state).toBe("disconnected");
    expect(states).toHaveBeenLastCalledWith("disconnected");
    expect(socket.error?.message).toBe(`WebSocket ticket authorization failed (HTTP ${status}). Check your API token and session access.`);
    expect(errors).toHaveBeenCalledWith(socket.error);
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.wsTicket).toHaveBeenCalledTimes(1); expect(FakeSocket.instances).toHaveLength(0);
    socket.connect(session.id); await vi.advanceTimersByTimeAsync(0);
    expect(socket.error).toBeNull(); expect(FakeSocket.instances).toHaveLength(1);
    socket.disconnect();
  });
  it.each(["network", 500, 503])("retries a %s ticket failure with normal backoff", async failure => {
    const error = failure === "network" ? new TypeError("Network unavailable") : new ApiError({ type: "about:blank", title: "Server error", status: Number(failure), detail: null, instance: null, errors: null });
    vi.mocked(api.wsTicket).mockRejectedValueOnce(error).mockRejectedValueOnce(error);
    const socket = new SessionSocket(); socket.connect(session.id, 7);
    await vi.advanceTimersByTimeAsync(0);
    expect(socket.state).toBe("reconnecting"); expect(socket.error).toBeNull();
    await vi.advanceTimersByTimeAsync(999); expect(api.wsTicket).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1); expect(api.wsTicket).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1999); expect(FakeSocket.instances).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(1); expect(api.wsTicket).toHaveBeenCalledTimes(3);
    expect(new URL(FakeSocket.instances[0].url).searchParams.get("since")).toBe("7");
    socket.disconnect();
  });
  it.each(["resolve", "reject"])("ignores a stale ticket %s after switching sessions or disconnecting", async result => {
    const pending = Promise.withResolvers<WsTicket>();
    vi.mocked(api.wsTicket).mockReturnValueOnce(pending.promise);
    const socket = new SessionSocket(); socket.connect("old", 10);
    socket.connect("new", 2); await vi.advanceTimersByTimeAsync(0);
    if (result === "resolve") pending.resolve(ticket());
    else pending.reject(new ApiError({ type: "about:blank", title: "Denied", status: 401, detail: null, instance: null, errors: null }));
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeSocket.instances).toHaveLength(1); expect(socket.error).toBeNull();
    expect(new URL(FakeSocket.instances[0].url).pathname).toBe("/api/v1/ws/sessions/new");
    const next = Promise.withResolvers<WsTicket>();
    vi.mocked(api.wsTicket).mockReturnValueOnce(next.promise);
    socket.connect("last"); socket.disconnect(); next.resolve(ticket());
    await vi.advanceTimersByTimeAsync(60000);
    expect(FakeSocket.instances).toHaveLength(1); expect(socket.state).toBe("disconnected");
  });
  it("isolates throwing event and thumbnail listeners", async () => {
    const error = new Error("Listener failed");
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const socket = new SessionSocket();
    const eventListener = vi.fn(); const thumbnailListener = vi.fn();
    socket.onEvent(() => { throw error; }); socket.onEvent(eventListener);
    socket.onThumbnail(() => { throw error; }); socket.onThumbnail(thumbnailListener);
    socket.connect(session.id); await vi.advanceTimersByTimeAsync(0);
    const event = makeEvent("session.created", { session }, 1);
    const thumbnail = { type: "thumbnail", agent_id: "a", jpeg_b64: "abc", step: 1 };
    expect(() => FakeSocket.instances[0].send({ type: "event", event })).not.toThrow();
    expect(eventListener).toHaveBeenCalledWith(event);
    expect(() => FakeSocket.instances[0].send(thumbnail)).not.toThrow();
    expect(thumbnailListener).toHaveBeenCalledWith(thumbnail);
    expect(log).toHaveBeenCalledTimes(2);
    expect(log).toHaveBeenNthCalledWith(1, "SessionSocket listener failed", error);
    expect(log).toHaveBeenNthCalledWith(2, "SessionSocket listener failed", error);
    socket.disconnect();
  });
  it.each([1008, 4000, 4403, 4999])("stops reconnecting after terminal close code %s", async code => {
    const socket = new SessionSocket(); socket.connect(session.id); await vi.advanceTimersByTimeAsync(0);
    FakeSocket.instances[0].onopen?.(); FakeSocket.instances[0].close(code);
    expect(socket.state).toBe("disconnected");
    await vi.advanceTimersByTimeAsync(60000); expect(FakeSocket.instances).toHaveLength(1);
    expect(api.wsTicket).toHaveBeenCalledTimes(1);
    socket.disconnect();
  });
  it("keeps backoff across connections that open without receiving a message", async () => {
    const socket = new SessionSocket(); socket.connect(session.id); await vi.advanceTimersByTimeAsync(0);
    FakeSocket.instances[0].onopen?.(); FakeSocket.instances[0].close();
    await vi.advanceTimersByTimeAsync(1000);
    FakeSocket.instances[1].onopen?.(); FakeSocket.instances[1].close();
    await vi.advanceTimersByTimeAsync(1999); expect(FakeSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1); expect(FakeSocket.instances).toHaveLength(3);
    FakeSocket.instances[2].onopen?.();
    FakeSocket.instances[2].send({ type: "event", event: makeEvent("session.created", { session }, 1) });
    FakeSocket.instances[2].close();
    await vi.advanceTimersByTimeAsync(999); expect(FakeSocket.instances).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(1); expect(FakeSocket.instances).toHaveLength(4);
    socket.disconnect();
  });

  it("resumes from last seq with backoff and emits typed events and thumbnails", async () => {
    const socket = new SessionSocket(); const events = vi.fn(); const thumbnails = vi.fn(); const states = vi.fn();
    socket.onEvent(events); socket.onThumbnail(thumbnails); socket.onState(states);
    socket.connect(session.id, 3); await vi.advanceTimersByTimeAsync(0);
    const first = FakeSocket.instances[0];
    expect(first.url).toContain("since=3"); first.onopen?.();
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 4) });
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 4) });
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 9, "other") });
    first.send({ type: "thumbnail", agent_id: "a", jpeg_b64: "abc", step: 1 });
    expect(events).toHaveBeenCalledTimes(1); expect(thumbnails).toHaveBeenCalledTimes(1); expect(socket.lastSeq).toBe(4);
    first.close(); expect(socket.state).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(1000); expect(FakeSocket.instances[1].url).toContain("since=4");
    expect(api.wsTicket).toHaveBeenCalledTimes(2);
    FakeSocket.instances[1].close(); await vi.advanceTimersByTimeAsync(1999); expect(FakeSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1); expect(FakeSocket.instances).toHaveLength(3);
    socket.disconnect(); await vi.runAllTimersAsync(); expect(FakeSocket.instances).toHaveLength(3); expect(states).toHaveBeenLastCalledWith("disconnected");
  });
  it("ignores malformed messages and unsubscribes listeners", async () => {
    const socket = new SessionSocket(); const listener = vi.fn(); const unsubscribe = socket.onEvent(listener);
    socket.connect(session.id); await vi.advanceTimersByTimeAsync(0);
    const ws = FakeSocket.instances[0];
    ws.onmessage?.({ data: "not json" }); ws.send({ type: "event", event: { seq: "bad" } });
    expect(socket.lastSeq).toBe(0); expect(listener).not.toHaveBeenCalled();
    unsubscribe(); ws.send({ type: "event", event: makeEvent("session.created", { session }, 1) });
    expect(listener).not.toHaveBeenCalled(); socket.disconnect();
  });
  it("cancels the previous session and resets its sequence", async () => {
    const socket = new SessionSocket(); socket.connect("first", 10); await vi.advanceTimersByTimeAsync(0); FakeSocket.instances[0].close();
    socket.connect("next", 2); await vi.advanceTimersByTimeAsync(0); await vi.advanceTimersByTimeAsync(30000);
    expect(FakeSocket.instances).toHaveLength(2); expect(socket.lastSeq).toBe(2); expect(new URL(FakeSocket.instances[1].url).pathname).toBe("/api/v1/ws/sessions/next"); expect(new URL(FakeSocket.instances[1].url).searchParams.get("since")).toBe("2");
    socket.disconnect();
  });
});
