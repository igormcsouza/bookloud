import { expect, test } from "@playwright/test";
import { FIXTURE_MARKER } from "./fixtures/reader-pdf";
import { login, uploadFixture, waitForTerminal } from "./support/session";

// **The highest-value spec the PR pipeline runs.**
//
// In a PR environment PARTIAL/NO_AUDIO is not an edge case -- it is the ONLY
// terminal state a book ever has (`get_speech_synthesizer()` gates on
// `ENVIRONMENT != "prod"` first and unconditionally, so every chunk fails with
// EXTERNAL_TTS_DISABLED). So this spec is testing constraint 2 directly:
// "show a simple message, but allow the user to continue reading the book or
// whatever they can with no external service."
//
// It is SKIPPED against local compose, where `SYNTHESIS_STUB_MODE=silent`
// makes the same book READY -- the degraded state simply does not occur there,
// and asserting it would be asserting a lie.

test.describe("degraded: a book with no audio", () => {
  test.skip(
    process.env.E2E_AUDIO === "1",
    "local compose runs SilentSynthesizer, so its books reach READY, not PARTIAL/NO_AUDIO",
  );

  test("stays fully readable, offers a retry, and mounts no audio at all", async ({ page }) => {
    await login(page);
    const bookId = await uploadFixture(page);

    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
    await waitForTerminal(page);

    // §9's rule 1: if chunk text exists, it renders. There is no state in
    // which an error screen replaces the reading pane.
    await expect(page.getByTestId("reading-pane")).toBeVisible();
    await expect(page.getByTestId("chunk-2")).toContainText(FIXTURE_MARKER);

    // Row 6's exact copy. This is the sentence a real user reads in this
    // situation, which is why it is asserted verbatim rather than by regex.
    await expect(page.getByTestId("audio-notice")).toContainText(
      "Text-to-speech is turned off in this environment. You can still read the book.",
    );

    // No <audio> element with a src is mounted at all -- that is what makes
    // play() a genuine no-op rather than a decode error, and it is why the
    // player bar is absent rather than merely disabled.
    await expect(page.locator("audio[src]")).toHaveCount(0);
    await expect(page.getByTestId("player-bar")).toHaveCount(0);

    // Clicking "Try audio again" must produce a 202 and rewind the book.
    const responsePromise = page.waitForResponse(
      (response) =>
        response.url().includes(`/books/${bookId}/resynthesize`) && response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Try audio again" }).click();
    const response = await responsePromise;
    expect(response.status()).toBe(202);

    const payload = await response.json();
    expect(payload.book.status).toBe("EXTRACTED");
    expect(payload.book.terminal).toBe(false);
    expect(payload.retriedChunks).toBe(payload.book.progress.chunksTotal);

    // The UI drops the returned book into poll state and resumes.
    await expect(page.getByTestId("audio-notice")).toContainText("Preparing audio", {
      timeout: 30_000,
    });
    // The text never went away while all that happened.
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER);

    // And the loop closes: the fan-out really was re-published and really was
    // consumed, so the book cycles back to the same terminal state.
    await waitForTerminal(page);
    await expect(page.getByTestId("audio-notice")).toContainText(
      "Text-to-speech is turned off in this environment.",
    );
  });
});
