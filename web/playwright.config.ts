import { defineConfig, devices } from "@playwright/test";
import { browserMicFile } from "./e2e/audio.ts";

/**
 * Browser tests against the sim stack (#149): the real dashboard, controller
 * and a MuJoCo daemon. tools/sim/run.py starts them and sets SIM_CONTROLLER:
 *
 *   uv run python tools/sim/run.py -- bash -c 'cd web && bunx playwright test'
 *
 * One worker: the tests share one simulated robot.
 *
 * The voice tests need the audio gateway too (run.py --audio sets SIM_AUDIO).
 * Their Chromium has a fake mic that plays a test tone.
 */
const voice = process.env.SIM_AUDIO
  ? [
      {
        name: "voice",
        testMatch: "voice.spec.ts",
        use: {
          ...devices["Desktop Chrome"],
          viewport: { width: 1280, height: 800 },
          permissions: ["microphone"],
          launchOptions: {
            args: [
              "--use-fake-ui-for-media-stream",
              "--use-fake-device-for-media-stream",
              `--use-file-for-fake-audio-capture=${browserMicFile()}`,
              "--autoplay-policy=no-user-gesture-required",
            ],
          },
        },
      },
    ]
  : [];

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
    { name: "desktop", testIgnore: "voice.spec.ts", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "phone", testIgnore: "voice.spec.ts", use: { ...devices["Pixel 7"] } },
    ...voice,
  ],
});
