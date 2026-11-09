import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { health, profiles, session } from "@/mocks/fixtures";
import HomePage from "./page";
const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/lib/api", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { profiles: vi.fn(), sessions: vi.fn(), providerHealth: vi.fn(), createSession: vi.fn() } };
});
function renderPage() { return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}><HomePage /></QueryClientProvider>); }
async function ready() { renderPage(); await screen.findByRole("option", { name: "local" }); }
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.profiles).mockResolvedValue(profiles);
  vi.mocked(api.sessions).mockResolvedValue({ items: [session], next_cursor: null });
  vi.mocked(api.providerHealth).mockResolvedValue(health);
  vi.mocked(api.createSession).mockResolvedValue(session);
});
describe("HomePage", () => {
  it("shows the configured app name, recent session links and provider health badges", async () => {
    await ready();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Agent Platform");
    expect(await screen.findByRole("link", { name: /Research the next launch/ })).toHaveAttribute("href", `/sessions/${session.id}/plan`);
    expect(await screen.findByText(/docker_local · Healthy/)).toBeVisible();
    expect(screen.getByText(/e2b · Unavailable/)).toBeVisible();
    expect(api.sessions).toHaveBeenCalledWith({ limit: 10 }); expect(api.providerHealth).toHaveBeenCalled();
  });
  it("rejects whitespace prompts without a request", async () => {
    await ready(); await userEvent.type(screen.getByLabelText("What should your agents do?"), "   "); await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a prompt"); expect(api.createSession).not.toHaveBeenCalled();
  });
  it.each(["0", "7", "1.5"])("rejects invalid agent count %s", async value => {
    await ready(); fireEvent.change(screen.getByLabelText("What should your agents do?"), { target: { value: "Research" } }); fireEvent.change(screen.getByLabelText("Agents"), { target: { value } });
    await userEvent.click(screen.getByRole("button", { name: "Plan" })); expect(screen.getByRole("alert")).toHaveTextContent("Agent count"); expect(api.createSession).not.toHaveBeenCalled();
  });
  it("validates advanced budgets", async () => {
    await ready(); fireEvent.change(screen.getByLabelText("What should your agents do?"), { target: { value: "Research" } });
    await userEvent.click(screen.getByText("Advanced")); fireEvent.change(screen.getByLabelText("Total steps"), { target: { value: "0" } }); await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Budgets require whole numbers"); expect(api.createSession).not.toHaveBeenCalled();
  });
  it("submits selected settings and budgets and navigates to plan review", async () => {
    await ready(); await userEvent.type(screen.getByLabelText("What should your agents do?"), "  Compare launch options  ");
    await userEvent.selectOptions(screen.getByLabelText("Sandbox provider"), "e2b"); await userEvent.click(screen.getByText("Advanced"));
    await userEvent.selectOptions(screen.getByLabelText("Mode"), "parallel_isolated"); fireEvent.change(screen.getByLabelText("Cost budget (USD, optional)"), { target: { value: "5.5" } });
    await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/sessions/${session.id}/plan`));
    expect(api.createSession).toHaveBeenCalledWith({ prompt: "Compare launch options", agent_count: 3, model_profile: "local", sandbox_provider: "e2b", mode: "parallel_isolated", budgets: { max_steps_per_task: 150, max_total_steps: 1500, max_wall_minutes: 60, max_cost_usd: 5.5, max_tokens: null } }, expect.anything());
  });
  it("shows create errors and supports retry", async () => {
    vi.mocked(api.createSession).mockRejectedValueOnce(new ApiError({ type: "about:blank", title: "Unavailable", status: 503, detail: "Planner offline", instance: null, errors: null }));
    await ready(); fireEvent.change(screen.getByLabelText("What should your agents do?"), { target: { value: "Research" } }); await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Planner offline"); expect(push).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Plan" })); await waitFor(() => expect(push).toHaveBeenCalled());
  });
  it("disables submission until profiles load", async () => {
    vi.mocked(api.profiles).mockReturnValue(new Promise(() => {})); renderPage(); expect(screen.getByRole("button", { name: "Plan" })).toBeDisabled();
  });
  it("prevents duplicate submissions while a session is being created", async () => {
    let complete: ((value: typeof session) => void) | undefined;
    vi.mocked(api.createSession).mockReturnValue(new Promise(resolve => { complete = resolve; }));
    await ready();
    fireEvent.change(screen.getByLabelText("What should your agents do?"), { target: { value: "Research" } });
    await userEvent.click(screen.getByRole("button", { name: "Plan" }));
    expect(screen.getByRole("button", { name: "Planning…" })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText("What should your agents do?"), { key: "Enter", ctrlKey: true });
    expect(api.createSession).toHaveBeenCalledTimes(1);
    complete?.(session);
    await waitFor(() => expect(push).toHaveBeenCalled());
  });
  it("shows empty and failed server state clearly", async () => {
    vi.mocked(api.sessions).mockResolvedValue({ items: [], next_cursor: null }); vi.mocked(api.providerHealth).mockRejectedValue(new Error("Health offline")); vi.mocked(api.profiles).mockResolvedValue({ items: [], default_profile: null });
    renderPage(); expect(await screen.findByText("No sessions yet")).toBeVisible(); expect(await screen.findByRole("alert")).toHaveTextContent("Health offline"); expect(screen.getByRole("button", { name: "Plan" })).toBeDisabled();
  });
});
