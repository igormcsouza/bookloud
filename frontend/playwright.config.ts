import { defineConfig, devices } from "@playwright/test";

// End-to-end tests run against a locally running stack (`make up && make ui`).
// No specs exist yet — scaffolded now, the first real spec lands in phase 6
// (reader UI) per IMPLEMENTATION_PLAN.md.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
