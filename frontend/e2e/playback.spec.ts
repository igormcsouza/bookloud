import { expect, test } from "@playwright/test";
import { FIXTURE_MARKER } from "./fixtures/reader-pdf";
import { login, uploadFixture, waitForTerminal } from "./support/session";

// The ONLY test anywhere in this repo that hears anything.
//
// It runs exclusively in the `playback` project, which only exists when
// `E2E_AUDIO=1` -- i.e. against local compose, where
// `SYNTHESIS_STUB_MODE=silent` puts SilentSynthesizer on the
// synthesize-worker and produces real MPEG-2 Layer III frames plus real
// interpolated word marks. Every deployed environment leaves that unset and
// has no audio at all to play.
//
// `channel: "chrome"` (playwright.config.ts): Playwright's bundled Chromium
// omits proprietary media codecs. The `canPlayType` assertion below is a
// HARD FAIL rather than a skip, because a silent skip in the one test that
// exercises audio would be worse than having no test (OQ-7).

test.describe("playback and highlight sync", () => {
  test("plays, advances the highlight, seeks across a segment boundary and back", async ({
    page,
  }) => {
    await login(page);
    const bookId = await uploadFixture(page);

    const canPlay = await page.evaluate(() =>
      document.createElement("audio").canPlayType("audio/mpeg"),
    );
    expect(
      canPlay,
      "this browser cannot decode audio/mpeg; the playback project must run channel: 'chrome'",
    ).not.toBe("");

    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
    await waitForTerminal(page);

    // With silent synthesis the book is READY, so the player is present and
    // enabled and a src really is mounted.
    const player = page.getByTestId("player-bar");
    await expect(player).toBeVisible();
    const audio = page.getByTestId("player-audio");
    await expect(audio).toHaveAttribute("src", /X-Amz-Signature/, { timeout: 30_000 });

    // The presigned URL is fetched by the media element itself, with no
    // Authorization header -- and it decodes. `duration` being finite is the
    // first thing that proves the whole §3 chain end to end.
    await expect
      .poll(
        async () =>
          audio.evaluate((element) => (element as HTMLAudioElement).readyState),
        { timeout: 30_000 },
      )
      .toBeGreaterThan(0);

    // --- play: the highlighted word must advance -------------------------
    await page.getByTestId("play-toggle").click();
    await expect(page.getByTestId("active-word")).toBeVisible({ timeout: 15_000 });
    const firstWord = await page.getByTestId("active-word").innerText();

    await expect
      .poll(async () => page.getByTestId("active-word").innerText(), {
        timeout: 15_000,
        intervals: [250],
      })
      .not.toBe(firstWord);

    // --- pause ------------------------------------------------------------
    await page.getByTestId("play-toggle").click();
    await expect(page.getByTestId("play-toggle")).toHaveAccessibleName("Play");
    const pausedWord = await page.getByTestId("active-word").innerText();
    await page.waitForTimeout(1_500);
    expect(await page.getByTestId("active-word").innerText()).toBe(pausedWord);

    // Seek to a point comfortably inside the second chunk by using the
    // scrubber, then assert the ACTIVE PARAGRAPH is chunk 1 -- crossing a
    // segment boundary is the whole reason the search is two levels deep.
    const durationMs = await page
      .getByTestId("player-scrubber")
      .evaluate((element) => Number((element as HTMLInputElement).max));
    expect(durationMs).toBeGreaterThan(0);

    await page.getByTestId("player-scrubber").evaluate((element, value) => {
      const input = element as HTMLInputElement;
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(input, String(value));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }, Math.floor(durationMs * 0.3));

    await expect
      .poll(
        async () => {
          const active = page.locator("[data-active='true']");
          if ((await active.count()) === 0) return -1;
          const id = await active.first().getAttribute("data-testid");
          return Number(id?.replace("chunk-", "") ?? -1);
        },
        { timeout: 15_000 },
      )
      .toBeGreaterThan(0);

    // --- seek back to 0 ---------------------------------------------------
    await page.getByTestId("player-scrubber").evaluate((element) => {
      const input = element as HTMLInputElement;
      const setter = Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype,
        "value",
      )!.set!;
      setter.call(input, "0");
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });

    await expect(page.getByTestId("chunk-0")).toHaveAttribute("data-active", "true", {
      timeout: 15_000,
    });
  });

  test("playback rate does not shift the highlight (everything is media time)", async ({ page }) => {
    await login(page);
    await uploadFixture(page);
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
    await waitForTerminal(page);

    await page.getByTestId("player-rate").selectOption("2");
    await page.getByTestId("play-toggle").click();

    await expect(page.getByTestId("active-word")).toBeVisible({ timeout: 15_000 });
    const first = await page.getByTestId("active-word").innerText();
    await expect
      .poll(async () => page.getByTestId("active-word").innerText(), {
        timeout: 15_000,
        intervals: [250],
      })
      .not.toBe(first);

    // The word under the highlight must still be a real slice of the active
    // paragraph's text -- a rate change must not desync the mapping, because
    // audio.currentTime is media time, not wall-clock time.
    const active = page.locator("[data-active='true']").first();
    const paragraph = await active.innerText();
    const word = await page.getByTestId("active-word").innerText();
    expect(paragraph).toContain(word);
  });
});
