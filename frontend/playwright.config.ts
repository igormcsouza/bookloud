import { defineConfig, devices } from "@playwright/test";

// End-to-end tests run against a locally running stack (`make up && make ui`)
// or against a deployed PR environment (`E2E_BASE_URL`).
//
// **Two projects, because exactly one environment in existence can produce
// audio** (PLANS/phase-6.md §13.4). `get_speech_synthesizer()` gates on
// `ENVIRONMENT != "prod"` first and unconditionally, so in `deploy-pr` every
// chunk reaches FAILED/EXTERNAL_TTS_DISABLED, the book lands PARTIAL/NO_AUDIO
// and `audioKey` is null; `deploy-prod.yml` never runs login-gated checks at
// all. Local compose is the only place a Playwright test can hear anything,
// and only because `SYNTHESIS_STUB_MODE=silent` puts SilentSynthesizer (real
// MPEG-2 frames, real interpolated word marks) on the synthesize-worker.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  // A deployed PR environment is a cold Lambda behind CloudFront; a local
  // compose stack is a poll loop waiting on a real pipeline.
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "on-first-retry",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
      // Runs everywhere and asserts NOTHING about audio.
      testIgnore: /playback\.spec\.ts/,
    },
    // `channel: "chrome"` -- Playwright's bundled Chromium omits proprietary
    // media codecs, and MP3 sits in the grey zone across versions. Google
    // Chrome ships them. This project is the ONLY place audio is ever
    // exercised, so it must not be the place a codec gap silently turns a real
    // assertion into a skip; playback.spec.ts hard-fails if
    // canPlayType("audio/mpeg") comes back empty (OQ-7).
    ...(process.env.E2E_AUDIO === "1"
      ? [
          {
            name: "playback",
            use: { ...devices["Desktop Chrome"], channel: "chrome" },
            testMatch: /playback\.spec\.ts/,
          },
        ]
      : []),
  ],
});
