import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { eventPayloads, makeEvent, session } from "@/mocks/fixtures";
import { SessionSocket } from "./ws";
vi.mock("./env", () => ({ env: { wsUrl: "ws://api.example", mock: false } }));
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  constructor(public url: string) { FakeSocket.instances.push(this); }
  close() { this.onclose?.(); }
  send(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) }); }
}
beforeEach(() => { vi.useFakeTimers(); FakeSocket.instances = []; vi.stubGlobal("WebSocket", FakeSocket); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
describe("SessionSocket", () => {
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
