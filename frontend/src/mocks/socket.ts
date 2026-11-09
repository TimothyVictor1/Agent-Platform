import type { WsServerMessage } from "@/contracts/generated";
import { scriptedEvents } from "./fixtures";
import { isReviewSession, reviewEvents } from "./plan";

// Tiny JPEG thumbnail. Mock mode never connects to an external stream.
export const thumbnailJpeg = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABAf/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k=";
export function openMockSocket(sessionId: string, since: number, receive: (message: WsServerMessage) => void) {
  if (isReviewSession(sessionId) && reviewEvents(sessionId)) {
    let seq = since;
    const timer = setInterval(() => {
      for (const event of reviewEvents(sessionId) ?? []) {
        if (event.seq <= seq) continue;
        receive({ type: "event", event }); seq = event.seq;
      }
    }, 100);
    return () => clearInterval(timer);
  }
  const events = scriptedEvents(sessionId);
  let index = events.findIndex(event => event.seq > since);
  const timer = setInterval(() => {
    if (index < 0 || index >= events.length) { clearInterval(timer); return; }
    const event = events[index++];
    receive({ type: "event", event });
    if (event.type === "agent.step") receive({ type: "thumbnail", agent_id: event.payload.agent_id, jpeg_b64: thumbnailJpeg, step: event.payload.step });
  }, 300);
  return () => clearInterval(timer);
}
