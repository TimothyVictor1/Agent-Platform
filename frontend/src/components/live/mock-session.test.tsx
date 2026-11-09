import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, configure, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LiveSession } from "./live-session";

vi.setConfig({ testTimeout: 30000 });
configure({ asyncUtilTimeout: 10000 });
// Exercise the real api.ts, ws.ts and store against the unmodified T05 script.
vi.mock("@/lib/env", () => ({ env: { mock: true, apiUrl: "http://localhost:8000", wsUrl: "ws://localhost:8000" } }));
afterEach(() => { vi.useRealTimers(); localStorage.clear(); });

it("plays the full 3-agent mock session through completion without external streams", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><LiveSession sessionId="demo-session" /></QueryClientProvider>);
  await screen.findByRole("heading", { name: "Research the next launch" });
  await waitFor(() => expect(screen.queryByText(/Resume from event/)).not.toBeInTheDocument());
  vi.useFakeTimers();
  act(() => { vi.advanceTimersByTime(60000); });
  expect(screen.getAllByRole("article", { name: / agent$/ })).toHaveLength(3);
  expect(screen.getByRole("link", { name: "View Replay" })).toHaveAttribute("href", "/sessions/demo-session/replay");
  expect(screen.getByRole("button", { name: "Download report.txt" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  expect(screen.getByText(/180 \/ 1500 steps/)).toBeVisible();
  expect(view.container.querySelector("iframe")).toBeNull();
  expect(screen.getAllByRole("img", { name: /latest desktop thumbnail/ })).toHaveLength(3);
  view.unmount();
});
