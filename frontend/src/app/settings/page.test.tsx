import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { health, profiles } from "@/mocks/fixtures";
import SettingsPage from "./page";
vi.mock("@/lib/api", () => ({ api: { profiles: vi.fn(async () => profiles), providerHealth: vi.fn(async () => health) } }));
describe("Settings", () => {
  it("shows read-only role summaries, health and the YAML configuration paths", async () => {
    render(<QueryClientProvider client={new QueryClient()}><SettingsPage /></QueryClientProvider>);
    expect(await screen.findByRole("table", { name: "Roles in local" })).toBeVisible();
    expect(screen.getByText("demo-worker")).toBeVisible(); expect(await screen.findByText(/docker_local · Healthy/)).toBeVisible();
    expect(screen.getByText("config/models.example.yaml")).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
});
