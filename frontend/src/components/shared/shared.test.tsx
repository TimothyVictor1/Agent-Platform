import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { budgets } from "@/mocks/fixtures";
import { emptyUsage } from "@/lib/session-store";
import { AgentAvatar } from "./agent-avatar";
import { CostMeter } from "./cost-meter";
import { StatusChip } from "./status-chip";
import { AppNav } from "../app-nav";
vi.mock("next/navigation", () => ({ usePathname: () => "/" }));
describe("shared shell", () => {
  it("labels agents and statuses without relying on color", () => {
    render(<><AgentAvatar name="Scout" color="#38bdf8" /><StatusChip status="awaiting_approval" /></>);
    expect(screen.getByText("Scout")).toBeVisible(); expect(screen.getByText("awaiting approval")).toBeVisible();
  });
  it("shows budget progress, including zero and unlimited cost budgets", () => {
    const { rerender } = render(<CostMeter usage={{ ...emptyUsage(), cost_usd: 5 }} budgets={{ ...budgets, max_cost_usd: 10 }} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
    rerender(<CostMeter usage={emptyUsage()} budgets={{ ...budgets, max_cost_usd: 0 }} />); expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
    rerender(<CostMeter usage={{ ...emptyUsage(), steps: 750 }} budgets={budgets} />); expect(screen.getByRole("progressbar", { name: "Step budget used" })).toHaveAttribute("aria-valuenow", "50");
  });
  it("defaults to dark and toggles the theme with an accessible button", async () => {
    document.documentElement.classList.add("dark"); render(<AppNav />);
    expect(screen.getByRole("link", { name: "Sessions" })).toHaveAttribute("aria-current", "page");
    const toggle = screen.getByRole("button", { name: "Dark theme" }); expect(toggle).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(toggle); expect(document.documentElement).not.toHaveClass("dark"); expect(toggle).toHaveAttribute("aria-pressed", "false"); expect(toggle).toHaveAccessibleName("Dark theme"); await userEvent.click(toggle); expect(document.documentElement).toHaveClass("dark"); expect(toggle).toHaveAttribute("aria-pressed", "true");
  });
});
