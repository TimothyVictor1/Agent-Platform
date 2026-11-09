import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SessionDetail } from "@/contracts/generated";
import { api } from "@/lib/api";
import { SessionSocket } from "@/lib/ws";
import { session } from "@/mocks/fixtures";
import { SessionPlaceholder } from "./session-placeholder";

vi.mock("@/lib/api", () => ({ api: { session: vi.fn() } }));
vi.mock("@/lib/ws", () => ({ SessionSocket: vi.fn(class {
  onEvent = vi.fn();
  onState = vi.fn();
  connect = vi.fn();
  disconnect = vi.fn();
}) }));

describe("SessionPlaceholder", () => {
  it("hydrates refetched detail without closing or reopening the session socket", async () => {
    const detail: SessionDetail = { session, plan: null, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 5 };
    vi.mocked(api.session).mockResolvedValueOnce(detail).mockResolvedValueOnce({ ...detail, session: { ...session, title: "Refetched title" } });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><SessionPlaceholder sessionId={session.id} title="Live session" /></QueryClientProvider>);
    await screen.findByText(session.title);
    await waitFor(() => expect(SessionSocket).toHaveBeenCalledTimes(1));
    const socket = vi.mocked(SessionSocket).mock.results[0].value as SessionSocket;
    expect(socket.connect).toHaveBeenCalledWith(session.id, 5);
    await act(async () => { await client.refetchQueries({ queryKey: ["session", session.id] }); });
    expect(await screen.findByText("Refetched title")).toBeVisible();
    expect(SessionSocket).toHaveBeenCalledTimes(1);
    expect(socket.connect).toHaveBeenCalledTimes(1);
    expect(socket.disconnect).not.toHaveBeenCalled();
    view.unmount();
    expect(socket.disconnect).toHaveBeenCalledTimes(1);
  });
});
