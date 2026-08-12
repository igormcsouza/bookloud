import { expect, test } from "@playwright/test";
import { FIXTURE_BODY_PHRASE, FIXTURE_MARKER } from "./fixtures/reader-pdf";
import { login, uploadFixture, waitForTerminal } from "./support/session";

// Runs in BOTH environments (`e2e-local` against compose, `deploy-pr` against
// real AWS) and asserts **nothing about audio** -- deliberately, because the
// two environments disagree about whether audio exists and this spec is about
// the path that is identical in both.
//
// What it proves in `deploy-pr` specifically: Cognito login through the SSR
// Lambda's BFF handlers, CloudFront -> Lambda Function URL, a presigned S3
// upload from a real browser to real S3, the extract -> synthesize -> stitch
// pipeline, the poll loop, and the reading pane rendering text a real PyMuPDF
// Lambda extracted.

test.describe("upload and read", () => {
  test("uploads a PDF, polls to terminal, and renders the extracted text", async ({ page }) => {
    await login(page);

    const bookId = await uploadFixture(page);

    // The text is readable from EXTRACTED onward -- extraction writes chunks
    // BEFORE the status flip, and making the user wait for synthesis to read
    // is precisely what constraint 2 forbids. So this must pass long before
    // the book is terminal.
    await expect(page.getByTestId("reading-pane")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_BODY_PHRASE);

    // The fixture is deliberately >= 3 chunks: crossing a segment boundary is
    // the assertion the two-level binary search exists for.
    await expect(page.getByTestId("chunk-2")).toBeVisible();

    await waitForTerminal(page);

    // The sidebar and the reader must agree -- they poll separately (5 s for
    // the list, 2 s for the open book) precisely so the badge is never stale.
    const row = page.getByTestId(`book-row-${bookId}`);
    await expect(row).toBeVisible();
    await expect
      .poll(async () => row.getAttribute("data-status"), { timeout: 30_000 })
      .toMatch(/READY|PARTIAL|FAILED/);

    // A terminal book shows no percentage -- "100%" forever is noise, and a
    // percentage on a PARTIAL book would imply it is still going.
    await expect(page.getByTestId(`book-progress-${bookId}`)).toHaveCount(0);
  });

  test("the reader survives a reload with the text intact", async ({ page }) => {
    await login(page);
    const bookId = await uploadFixture(page);
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });

    await page.reload();

    // A full reload drops the in-memory id token (it lives in a browser
    // module variable by design), so this also proves the silent refresh off
    // the httpOnly cookie works from a cold page.
    await expect(page).toHaveURL(new RegExp(`/books/${bookId}`));
    await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
  });
});
