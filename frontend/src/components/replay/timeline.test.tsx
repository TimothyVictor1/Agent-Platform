import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { largeReplayFixture } from "@/mocks/replay";
import { buildReplay } from "./model";
import { Timeline } from "./timeline";

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("virtual replay timeline", () => {
  it("mounts a bounded window from 6 × 500 steps and fetches frames only on intersection", async () => {
    const fixture = largeReplayFixture();
    const model = buildReplay(fixture.events, fixture.frames, fixture.agents);
    const observers: { callback: IntersectionObserverCallback; element?: Element; disconnected: boolean }[] = [];
    vi.stubGlobal("IntersectionObserver", class {
      record: typeof observers[number];
      constructor(callback: IntersectionObserverCallback) { this.record = { callback, disconnected: false }; observers.push(this.record); }
      observe(element: Element) { this.record.element = element; }
      disconnect() { this.record.disconnected = true; }
    });
    const fetchFrame = vi.spyOn(api, "frame").mockResolvedValue(new Blob(["jpeg"], { type: "image/jpeg" }));
    const createUrl = vi.fn(() => "blob:replay-frame"), revokeUrl = vi.fn();
    vi.stubGlobal("URL", class extends URL { static createObjectURL = createUrl; static revokeObjectURL = revokeUrl; });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const onFrame = vi.fn(), onSelect = vi.fn();
    const view = render(<QueryClientProvider client={client}><Timeline model={model} position={model.start} onFrame={onFrame} onSelect={onSelect} /></QueryClientProvider>);
    expect(screen.getByLabelText("Supervisor lane")).toBeInTheDocument();
    expect(screen.getAllByTestId("timeline-thumbnail").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("timeline-thumbnail").length).toBeLessThanOrEqual(90);
    expect(fetchFrame).not.toHaveBeenCalled();
    await act(async () => {
      observers[0].callback([{ isIntersecting: true, target: observers[0].element } as IntersectionObserverEntry], {} as IntersectionObserver);
    });
    await waitFor(() => expect(fetchFrame).toHaveBeenCalledTimes(1));
    expect(fetchFrame).toHaveBeenCalledWith(fixture.frames[0].id);
    await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Ada, step 1: Read source 1" }));
    expect(onFrame).toHaveBeenCalledWith(fixture.frames[0]);
    const region = screen.getByRole("region", { name: "Scrollable replay lanes" });
    fireEvent.scroll(region, { target: { scrollLeft: model.width / 2 } });
    expect(screen.getAllByTestId("timeline-thumbnail").length).toBeLessThanOrEqual(90);
    expect(screen.queryByRole("button", { name: "Ada, step 1: Read source 1" })).not.toBeInTheDocument();
    expect(fetchFrame).toHaveBeenCalledTimes(1);
    expect(revokeUrl).toHaveBeenCalledWith("blob:replay-frame");
    const nextObserver = observers.find(record => !record.disconnected && record.element?.isConnected)!;
    await act(async () => nextObserver.callback([{ isIntersecting: true, target: nextObserver.element } as IntersectionObserverEntry], {} as IntersectionObserver));
    await waitFor(() => expect(fetchFrame).toHaveBeenCalledTimes(2));
    expect(fetchFrame.mock.calls[1][0]).not.toBe(fixture.frames[0].id);
    const marker = screen.getAllByRole("button", { name: /fact: Source 250/ })[0];
    fireEvent.click(marker); expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ type: "board.entry_posted" }));
    expect(screen.getByLabelText("Message from Ada to Brook")).toHaveAttribute("d", expect.stringContaining(" C "));
    view.unmount(); client.clear();
    expect(observers.every(record => record.disconnected)).toBe(true);
  }, 30000);
});
