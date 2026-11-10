import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { LiveSession } from "./live-session";
import { api } from "@/lib/api";

vi.setConfig({ testTimeout: 30000 });
// Exercise the real api.ts, ws.ts and store against the unmodified T05 script.
vi.mock("@/lib/env", () => ({ env: { mock: true, apiUrl: "http://localhost:8000", wsUrl: "ws://localhost:8000" } }));
afterEach(() => { vi.useRealTimers(); localStorage.clear(); });

it("plays the full 3-agent mock session through completion without external streams", async () => {
  // demo-session is reserved for T12 plan review; created sessions use the live script.
  const session = await api.createSession({ prompt: "Research the next launch", agent_count: 3, model_profile: "local", sandbox_provider: "docker_local" });
  // Warm dynamic modules, then install timers before the socket starts its interval.
  await api.session(session.id);
  await import("@/mocks/socket");
  vi.useFakeTimers();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><LiveSession sessionId={session.id} /></QueryClientProvider>);
  // REST hydration and the socket's dynamic import settle in separate effects.
  for (let attempt = 0; attempt < 10 && screen.queryByText(/Resume from event/); attempt++) {
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
  }
  expect(screen.getByRole("heading", { name: "Research the next launch" })).toBeVisible();
  expect(screen.queryByText(/Resume from event/)).not.toBeInTheDocument();
  act(() => { vi.advanceTimersByTime(60000); });
  expect(screen.getAllByRole("article", { name: / agent$/ })).toHaveLength(3);
  expect(screen.getByRole("link", { name: "View Replay" })).toHaveAttribute("href", `/sessions/${session.id}/replay`);
  expect(screen.getByRole("button", { name: "Download report.txt" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Stop" })).toBeDisabled();
  expect(screen.getByText(/180 \/ 1500 steps/)).toBeVisible();
  expect(view.container.querySelector("iframe")).toBeNull();
  expect(screen.getAllByRole("img", { name: /latest desktop thumbnail/ })).toHaveLength(3);
  view.unmount();
});
