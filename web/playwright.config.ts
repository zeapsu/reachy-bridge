import { defineConfig, devices } from "@playwright/test";

/**
 * Browser tests against the sim stack (#149): the real dashboard, controller
 * and a MuJoCo daemon. tools/sim/run.py starts them and sets SIM_CONTROLLER:
 *
 *   uv run python tools/sim/run.py -- bash -c 'cd web && bunx playwright test'
 *
 * One worker: the tests share one simulated robot.
 */
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 30_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: process.env.SIM_CONTROLLER ?? "http://127.0.0.1:7862",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
