import { expect, test } from "@playwright/test";

test("mock plan supports drag-to-connect and keyboard node selection", async ({ page }) => {
  await page.goto("/sessions/demo-session/plan");
  const graph = page.getByRole("region", { name: "Plan dependency graph" });
  const task = graph.getByRole("group", { name: "Task T1: Research launch options", exact: true });
  await expect(task).toBeVisible();
  await task.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "Edit task T1" })).toBeVisible();

  const source = graph.getByLabel("Output source for T1", { exact: true });
  const target = graph.getByLabel("Dependency target for T2", { exact: true });
  await source.scrollIntoViewIfNeeded();
  const start = await source.boundingBox(); const end = await target.boundingBox();
  if (!start || !end) throw new Error("Task connection handles must be visible.");
  await page.mouse.move(start.x + start.width / 2, start.y + start.height / 2);
  await page.mouse.down();
  await page.mouse.move(end.x + end.width / 2, end.y + end.height / 2, { steps: 15 });
  await page.mouse.up();
  await expect(graph.getByRole("group", { name: "Dependency T1 to T2", exact: true })).toBeVisible();

  const downstream = graph.getByRole("group", { name: "Task T2: Gather pricing evidence", exact: true });
  await downstream.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("complementary", { name: "Edit task T2" })).toBeVisible();
  await expect(page.getByLabel("Dependencies (select multiple)")).toHaveValues(["T1"]);
});
