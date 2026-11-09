import type { Event, WsServerMessage, WsThumbnailMessage } from "@/contracts/generated";
import { env } from "./env";

export type ConnectionState = "disconnected" | "connecting" | "connected" | "reconnecting";
/** One socket per session. Subscribers survive reconnects; disconnect cancels all timers. */
export class SessionSocket {
  private socket: WebSocket | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private cancelMock: (() => void) | null = null;
  private generation = 0;
  private sessionId = "";
  private seq = 0;
  private attempts = 0;
  private stopped = true;
  private currentState: ConnectionState = "disconnected";
  private readonly events = new Set<(event: Event) => void>();
  private readonly thumbnails = new Set<(thumbnail: WsThumbnailMessage) => void>();
  private readonly states = new Set<(state: ConnectionState) => void>();
  get state() { return this.currentState; }
  get lastSeq() { return this.seq; }
  onEvent(listener: (event: Event) => void) { this.events.add(listener); return () => { this.events.delete(listener); }; }
  onThumbnail(listener: (thumbnail: WsThumbnailMessage) => void) { this.thumbnails.add(listener); return () => { this.thumbnails.delete(listener); }; }
  onState(listener: (state: ConnectionState) => void) { this.states.add(listener); listener(this.currentState); return () => { this.states.delete(listener); }; }
  private setState(state: ConnectionState) { this.currentState = state; this.states.forEach(listener => listener(state)); }
  connect(sessionId: string, since = 0) {
    this.disconnect();
    this.sessionId = sessionId; this.seq = since; this.attempts = 0; this.stopped = false;
    this.open(this.generation);
  }
  disconnect() {
    this.stopped = true; this.generation++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null; this.cancelMock?.(); this.cancelMock = null;
    if (this.socket) { this.socket.onclose = null; this.socket.onmessage = null; this.socket.onerror = null; this.socket.onopen = null; this.socket.close(); this.socket = null; }
    this.setState("disconnected");
  }
  private receive(message: WsServerMessage) {
    if (message.type === "event") {
      if (message.event.session_id !== this.sessionId || message.event.seq <= this.seq) return;
      this.seq = message.event.seq;
      this.events.forEach(listener => listener(message.event));
    } else { this.thumbnails.forEach(listener => listener(message)); }
  }
  private open(generation: number) {
    if (this.stopped || generation !== this.generation) return;
    this.setState(this.attempts ? "reconnecting" : "connecting");
    if (env.mock) {
      void import("@/mocks/socket").then(({ openMockSocket }) => {
        if (this.stopped || generation !== this.generation) return;
        this.setState("connected");
        this.cancelMock = openMockSocket(this.sessionId, this.seq, message => this.receive(message));
      });
      return;
    }
    try {
      const url = new URL(`${env.wsUrl.replace(/\/$/, "")}/api/v1/ws/sessions/${encodeURIComponent(this.sessionId)}`);
      url.searchParams.set("since", String(this.seq));
      // Browser WebSocket cannot set Authorization. Do not put bearer secrets in URLs.
      const socket = new WebSocket(url.toString());
      this.socket = socket;
      socket.onopen = () => { this.attempts = 0; this.setState("connected"); };
      socket.onmessage = ({ data }: MessageEvent<unknown>) => {
        if (typeof data !== "string") return;
        try {
          const message: unknown = JSON.parse(data);
          if (isServerMessage(message)) this.receive(message);
        } catch { /* Ignore invalid transport messages without changing the cursor. */ }
      };
      socket.onerror = () => socket.close();
      socket.onclose = () => { this.socket = null; this.reconnect(generation); };
    } catch { this.reconnect(generation); }
  }
  private reconnect(generation: number) {
    if (this.stopped || generation !== this.generation) return;
    this.setState("reconnecting");
    const delay = Math.min(1000 * 2 ** this.attempts++, 30000);
    this.timer = setTimeout(() => this.open(generation), delay);
  }
}
function isServerMessage(value: unknown): value is WsServerMessage {
  if (!value || typeof value !== "object" || !("type" in value)) return false;
  if (value.type === "thumbnail") return "agent_id" in value && typeof value.agent_id === "string" && "jpeg_b64" in value && typeof value.jpeg_b64 === "string";
  if (value.type !== "event" || !("event" in value) || !value.event || typeof value.event !== "object") return false;
  const event = value.event;
  return "seq" in event && typeof event.seq === "number" && Number.isSafeInteger(event.seq) && event.seq > 0 && "session_id" in event && typeof event.session_id === "string" && "type" in event && typeof event.type === "string" && "payload" in event && event.payload !== null && typeof event.payload === "object";
}
