import { expect, test } from "@playwright/test";

test("home page renders the app name", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent sessions" })).toBeVisible();
  await expect(page.getByText("docker_local · Healthy")).toBeVisible();
});

test("mock Home creates a session, then opens every session route and Settings", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("What should your agents do?").fill("Prepare a launch report");
  await page.getByRole("button", { name: "Plan", exact: true }).click();
  await expect(page).toHaveURL(/\/sessions\/[^/]+\/plan$/);
  await expect(page.getByRole("heading", { name: "Plan review", exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("connected");
  await page.getByRole("link", { name: "Live session", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Live session", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Replay", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Replay", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Sessions", exact: true }).click();
  await expect(page.getByRole("link", { name: /Prepare a launch report/ })).toBeVisible();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("table", { name: "Roles in local" })).toBeVisible();
});
