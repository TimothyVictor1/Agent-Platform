import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.PORT ?? 3000);

/**
 * End-to-end tests (SPEC §21). Run against the dev server in mock mode by default:
 *   pnpm exec playwright install chromium   # once
 *   pnpm e2e
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: `http://localhost:${port}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `pnpm dev --port ${port}`,
    url: `http://localhost:${port}`,
    reuseExistingServer: !process.env.CI,
    env: { NEXT_PUBLIC_MOCK: "1" },
  },
});
