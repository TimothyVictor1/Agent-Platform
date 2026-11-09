import type { Event } from "@/contracts/generated";
import { api } from "@/lib/api";

/** Follow the server cursor, including pages containing no events. */
export async function loadEvents(sessionId: string, signal?: AbortSignal): Promise<Event[]> {
  const events = new Map<number, Event>();
  let cursor = 0;
  for (;;) {
    signal?.throwIfAborted();
    const page = await api.events(sessionId, { after_seq: cursor, limit: 500 });
    signal?.throwIfAborted();
    for (const event of page.items) events.set(event.seq, event);
    if (page.next_after_seq === null) break;
    if (page.next_after_seq <= cursor) throw new Error("The event cursor did not advance. Retry loading replay.");
    cursor = page.next_after_seq;
  }
  return [...events.values()].sort((a, b) => a.seq - b.seq);
}
