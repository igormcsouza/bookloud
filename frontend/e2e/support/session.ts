import { expect, type Page } from "@playwright/test";
import { FIXTURE_FILENAME, FIXTURE_MIME, fixturePdf } from "../fixtures/reader-pdf";

/** Credentials. Locally these are the seeded `dev` user (`local/setup.sh`);
 *  in `deploy-pr` they are the smoke-test credentials that workflow already
 *  provisions, passed in as env. */
export const USERNAME = process.env.E2E_USERNAME ?? "dev";
export const PASSWORD = process.env.E2E_PASSWORD ?? "devpassword";

/**
 * Log in through the real UI, not by injecting a token.
 *
 * That is deliberate: the login page talks to the Next.js BFF route handlers
 * (`app/api/auth/*`), which are the *only* thing that ever sees the refresh
 * cookie, and in `deploy-pr` those handlers run in the SSR Lambda behind
 * CloudFront. Faking the session would skip exactly the part of the stack
 * these specs exist to exercise.
 */
export async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/username/i).fill(USERNAME);
  await page.getByLabel(/password/i).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in|log in/i }).click();
  // The middleware bounces a logged-in request away from /login, so landing
  // anywhere else is the signal that auth actually took.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
  await expect(page.getByTestId("book-sidebar")).toBeVisible();
}

/**
 * Upload the fixture PDF and return the new book's id.
 *
 * Goes through the real presigned POST -- browser straight to S3 (or
 * LocalStack), no Authorization header on that request. In `deploy-pr` this
 * is a real browser writing to a real bucket, which is the single most
 * valuable thing the PR pipeline proves about this flow.
 */
export async function uploadFixture(page: Page): Promise<string> {
  await page.getByTestId("upload-input").setInputFiles({
    name: FIXTURE_FILENAME,
    mimeType: FIXTURE_MIME,
    buffer: fixturePdf(),
  });

  await page.waitForURL(/\/books\/[0-9a-f-]+$/, { timeout: 60_000 });
  const match = /\/books\/([0-9a-f-]+)/.exec(page.url());
  if (!match) throw new Error(`did not navigate to a reader URL: ${page.url()}`);
  return match[1];
}

/**
 * Wait until the book reaches a terminal state.
 *
 * **The stop condition is the server's `terminal` flag**, read off the same
 * poll the app itself uses -- never a client-side status list. Waiting for
 * the literal string "READY" would hang forever on a PARTIAL book, i.e. on
 * every book in every PR environment (phase-5 §8.1). Here that shows up as
 * "the notice stopped saying 'Preparing audio'".
 */
export async function waitForTerminal(page: Page, timeoutMs = 150_000): Promise<void> {
  await expect
    .poll(
      async () => {
        const notice = page.getByTestId("audio-notice");
        if ((await notice.count()) === 0) return "terminal";
        const text = (await notice.first().innerText()).trim();
        return /Reading your PDF|Preparing audio/.test(text) ? "pending" : "terminal";
      },
      { timeout: timeoutMs, intervals: [2_000] },
    )
    .toBe("terminal");
}
