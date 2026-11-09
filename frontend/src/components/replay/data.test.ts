import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { scriptedEvents } from "@/mocks/fixtures";
import { loadEvents } from "./data";

afterEach(() => vi.restoreAllMocks());
describe("paged replay data", () => {
  it("follows cursors and deduplicates boundary events", async () => {
    const events = scriptedEvents();
    const request = vi.spyOn(api, "events").mockResolvedValueOnce({ items: events.slice(0, 100), next_after_seq: 100 }).mockResolvedValueOnce({ items: events.slice(99), next_after_seq: null });
    expect(await loadEvents("demo-session")).toEqual(events);
    expect(request.mock.calls).toEqual([["demo-session", { after_seq: 0, limit: 500 }], ["demo-session", { after_seq: 100, limit: 500 }]]);
  });
  it("rejects stalled cursors and respects cancellation", async () => {
    const request = vi.spyOn(api, "events").mockResolvedValue({ items: [], next_after_seq: 0 });
    await expect(loadEvents("demo-session")).rejects.toThrow("cursor did not advance");
    request.mockClear();
    const controller = new AbortController(); controller.abort();
    await expect(loadEvents("demo-session", controller.signal)).rejects.toThrow();
    expect(request).not.toHaveBeenCalled();
  });
});
