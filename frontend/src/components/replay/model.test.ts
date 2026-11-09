import { describe, expect, it } from "vitest";
import { allEventFixtures, eventPayloads, makeEvent } from "@/mocks/fixtures";
import { largeReplayFixture } from "@/mocks/replay";
import { arrowEndpoints, boardSymbols, buildReplay, eventLane, frameAt, LANE_HEIGHT, playbackReducer, recipients, timeToX, xToTime, type Playback } from "./model";

describe("replay mapping", () => {
  it("maps timestamps to timeline coordinates and back, including empty ranges", () => {
    expect(timeToX(150, 100, 200, 1000)).toBe(500);
    expect(xToTime(500, 100, 200, 1000)).toBe(150);
    expect(timeToX(300, 100, 200, 1000)).toBe(1000);
    expect(timeToX(0, 100, 200, 1000)).toBe(0);
    expect(timeToX(100, 100, 100, 1000)).toBe(0);
    expect(xToTime(-10, 100, 200, 1000)).toBe(100);
  });
  it("anchors curved arrows on the author and recipient lane, including supervisor and self messages", () => {
    const arrow = arrowEndpoints(300, 0, 5);
    expect(arrow).toMatchObject({ x1: 300, x2: 300, y1: 24, y2: 5 * LANE_HEIGHT + 24 });
    expect(arrow.path).toBe("M 300 24 C 344 24, 344 644, 300 644");
    expect(arrowEndpoints(0, 6, 0).y1).toBe(6 * LANE_HEIGHT + 24);
    expect(arrowEndpoints(100, 1, 1).path).toContain("C 144");
    const entry = { ...eventPayloads["board.entry_posted"].entry, author: { type: "agent", id: "a", name: "A" } as const, to: ["b", "c"] };
    const event = makeEvent("board.entry_posted", { entry }, 1);
    expect(eventLane(event)).toBe("a"); expect(recipients(event)).toEqual(["b", "c"]);
    expect(recipients(makeEvent("board.entry_posted", eventPayloads["board.entry_posted"], 2))).toEqual([]);
    expect(new Set(Object.values(boardSymbols)).size).toBe(9);
  });
  it("indexes six lanes and 3,000 frames, picks latest earlier frame and closes task/takeover spans", () => {
    const fixture = largeReplayFixture();
    const model = buildReplay(fixture.events, fixture.frames, fixture.agents);
    expect(model.lanes).toHaveLength(7);
    expect(fixture.frames).toHaveLength(3000);
    const frames = model.framesByAgent.get(fixture.agents[0].id)!;
    expect(frames).toHaveLength(500);
    expect(frameAt(frames, model.start)).toBeNull();
    expect(frameAt(frames, model.start + 100500)?.step).toBe(100);
    expect(frameAt(frames, model.end + 1000)?.step).toBe(500);
    expect(model.spans.filter(span => span.kind === "task")).toHaveLength(6);
    expect(model.spans.find(span => span.kind === "takeover")).toMatchObject({ start: model.start + 100000, end: model.start + 110000 });
    expect(buildReplay(fixture.events, []).framesByAgent.get(fixture.agents[0].id)).toHaveLength(500);
  });
  it("handles every event contract and sessions without events", () => {
    expect(buildReplay(allEventFixtures, []).events).toHaveLength(allEventFixtures.length);
    expect(buildReplay([], [])).toMatchObject({ start: 0, end: 0, events: [], spans: [] });
  });
});

describe("playback state machine", () => {
  const initial: Playback = { position: 100, playing: false, speed: 1 };
  it("plays at each speed, stops at the end, and restarts from the beginning", () => {
    for (const speed of [1, 4, 16] as const) {
      let state = playbackReducer(initial, { type: "speed", speed });
      state = playbackReducer(state, { type: "toggle", start: 100, end: 1000 });
      state = playbackReducer(state, { type: "tick", elapsed: 10, end: 1000 });
      expect(state.position).toBe(100 + speed * 10);
      state = playbackReducer(state, { type: "tick", elapsed: 1000, end: 1000 });
      expect(state).toMatchObject({ position: 1000, playing: false });
      expect(playbackReducer(state, { type: "toggle", start: 100, end: 1000 })).toMatchObject({ position: 100, playing: true });
    }
  });
  it("pauses, clamps seeks, ignores paused ticks and cannot play empty sessions", () => {
    expect(playbackReducer(initial, { type: "tick", elapsed: 100, end: 1000 })).toEqual(initial);
    expect(playbackReducer(initial, { type: "toggle", start: 100, end: 100 })).toEqual(initial);
    expect(playbackReducer({ ...initial, playing: true }, { type: "toggle", start: 100, end: 1000 }).playing).toBe(false);
    expect(playbackReducer({ ...initial, playing: true }, { type: "seek", position: 2000, start: 100, end: 1000 })).toMatchObject({ position: 1000, playing: false });
    expect(playbackReducer(initial, { type: "seek", position: -5, start: 100, end: 1000 }).position).toBe(100);
  });
});
