import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { eventPayloads, makeEvent, session } from "@/mocks/fixtures";
import { SessionSocket } from "./ws";
vi.mock("./env", () => ({ env: { wsUrl: "ws://api.example", mock: false } }));
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
beforeEach(() => { vi.useFakeTimers(); FakeSocket.instances = []; vi.stubGlobal("WebSocket", FakeSocket); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("SessionSocket", () => {
  it("isolates throwing event and thumbnail listeners", () => {
    const error = new Error("Listener failed");
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const socket = new SessionSocket();
    const eventListener = vi.fn(); const thumbnailListener = vi.fn();
    socket.onEvent(() => { throw error; }); socket.onEvent(eventListener);
    socket.onThumbnail(() => { throw error; }); socket.onThumbnail(thumbnailListener);
    socket.connect(session.id);
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
  it.each([1008, 4000, 4401, 4999])("stops reconnecting after terminal close code %s", code => {
    const socket = new SessionSocket(); socket.connect(session.id);
    FakeSocket.instances[0].onopen?.(); FakeSocket.instances[0].close(code);
    expect(socket.state).toBe("disconnected");
    vi.advanceTimersByTime(60000); expect(FakeSocket.instances).toHaveLength(1);
    socket.disconnect();
  });
  it("keeps backoff across connections that open without receiving a message", () => {
    const socket = new SessionSocket(); socket.connect(session.id);
    FakeSocket.instances[0].onopen?.(); FakeSocket.instances[0].close();
    vi.advanceTimersByTime(1000);
    FakeSocket.instances[1].onopen?.(); FakeSocket.instances[1].close();
    vi.advanceTimersByTime(1999); expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1); expect(FakeSocket.instances).toHaveLength(3);
    FakeSocket.instances[2].onopen?.();
    FakeSocket.instances[2].send({ type: "event", event: makeEvent("session.created", { session }, 1) });
    FakeSocket.instances[2].close();
    vi.advanceTimersByTime(999); expect(FakeSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1); expect(FakeSocket.instances).toHaveLength(4);
    socket.disconnect();
  });

  it("resumes from last seq with backoff and emits typed events and thumbnails", () => {
    const socket = new SessionSocket(); const events = vi.fn(); const thumbnails = vi.fn(); const states = vi.fn();
    socket.onEvent(events); socket.onThumbnail(thumbnails); socket.onState(states);
    socket.connect(session.id, 3);
    const first = FakeSocket.instances[0];
    expect(first.url).toContain("since=3"); first.onopen?.();
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 4) });
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 4) });
    first.send({ type: "event", event: makeEvent("session.paused", eventPayloads["session.paused"], 9, "other") });
    first.send({ type: "thumbnail", agent_id: "a", jpeg_b64: "abc", step: 1 });
    expect(events).toHaveBeenCalledTimes(1); expect(thumbnails).toHaveBeenCalledTimes(1); expect(socket.lastSeq).toBe(4);
    first.close(); expect(socket.state).toBe("reconnecting");
    vi.advanceTimersByTime(1000); expect(FakeSocket.instances[1].url).toContain("since=4");
    FakeSocket.instances[1].close(); vi.advanceTimersByTime(1999); expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1); expect(FakeSocket.instances).toHaveLength(3);
    socket.disconnect(); vi.runAllTimers(); expect(FakeSocket.instances).toHaveLength(3); expect(states).toHaveBeenLastCalledWith("disconnected");
  });
  it("ignores malformed messages and unsubscribes listeners", () => {
    const socket = new SessionSocket(); const listener = vi.fn(); const unsubscribe = socket.onEvent(listener);
    socket.connect(session.id);
    const ws = FakeSocket.instances[0];
    ws.onmessage?.({ data: "not json" }); ws.send({ type: "event", event: { seq: "bad" } });
    expect(socket.lastSeq).toBe(0); expect(listener).not.toHaveBeenCalled();
    unsubscribe(); ws.send({ type: "event", event: makeEvent("session.created", { session }, 1) });
    expect(listener).not.toHaveBeenCalled(); socket.disconnect();
  });
  it("cancels the previous session and resets its sequence", () => {
    const socket = new SessionSocket(); socket.connect("first", 10); FakeSocket.instances[0].close();
    socket.connect("next", 2); vi.advanceTimersByTime(30000);
    expect(FakeSocket.instances).toHaveLength(2); expect(socket.lastSeq).toBe(2); expect(FakeSocket.instances[1].url).toContain("/next?since=2");
    socket.disconnect();
  });
});
