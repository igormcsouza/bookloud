# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: playback.spec.ts >> playback and highlight sync >> playback rate does not shift the highlight (everything is media time)
- Location: e2e/playback.spec.ts:120:7

# Error details

```
Test timeout of 180000ms exceeded.
```

```
Error: locator.innerText: Test timeout of 180000ms exceeded.
Call log:
  - waiting for getByTestId('active-word')

```

# Page snapshot

```yaml
- 'heading "Application error: a client-side exception has occurred while loading localhost (see the browser console for more information)." [level=2] [ref=e4]'
```

# Test source

```ts
  30  |       canPlay,
  31  |       "this browser cannot decode audio/mpeg; the playback project must run channel: 'chrome'",
  32  |     ).not.toBe("");
  33  | 
  34  |     await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
  35  |     await waitForTerminal(page);
  36  | 
  37  |     // With silent synthesis the book is READY, so the player is present and
  38  |     // enabled and a src really is mounted.
  39  |     const player = page.getByTestId("player-bar");
  40  |     await expect(player).toBeVisible();
  41  |     const audio = page.getByTestId("player-audio");
  42  |     await expect(audio).toHaveAttribute("src", /X-Amz-Signature/, { timeout: 30_000 });
  43  | 
  44  |     // The presigned URL is fetched by the media element itself, with no
  45  |     // Authorization header -- and it decodes. `duration` being finite is the
  46  |     // first thing that proves the whole §3 chain end to end.
  47  |     await expect
  48  |       .poll(
  49  |         async () =>
  50  |           audio.evaluate((element) => (element as HTMLAudioElement).readyState),
  51  |         { timeout: 30_000 },
  52  |       )
  53  |       .toBeGreaterThan(0);
  54  | 
  55  |     // --- play: the highlighted word must advance -------------------------
  56  |     await page.getByTestId("play-toggle").click();
  57  |     await expect(page.getByTestId("active-word")).toBeVisible({ timeout: 15_000 });
  58  |     const firstWord = await page.getByTestId("active-word").innerText();
  59  | 
  60  |     await expect
  61  |       .poll(async () => page.getByTestId("active-word").innerText(), {
  62  |         timeout: 15_000,
  63  |         intervals: [250],
  64  |       })
  65  |       .not.toBe(firstWord);
  66  | 
  67  |     // --- pause ------------------------------------------------------------
  68  |     await page.getByTestId("play-toggle").click();
  69  |     await expect(page.getByTestId("play-toggle")).toHaveAccessibleName("Play");
  70  |     const pausedWord = await page.getByTestId("active-word").innerText();
  71  |     await page.waitForTimeout(1_500);
  72  |     expect(await page.getByTestId("active-word").innerText()).toBe(pausedWord);
  73  | 
  74  |     // Seek to a point comfortably inside the second chunk by using the
  75  |     // scrubber, then assert the ACTIVE PARAGRAPH is chunk 1 -- crossing a
  76  |     // segment boundary is the whole reason the search is two levels deep.
  77  |     const durationMs = await page
  78  |       .getByTestId("player-scrubber")
  79  |       .evaluate((element) => Number((element as HTMLInputElement).max));
  80  |     expect(durationMs).toBeGreaterThan(0);
  81  | 
  82  |     await page.getByTestId("player-scrubber").evaluate((element, value) => {
  83  |       const input = element as HTMLInputElement;
  84  |       const setter = Object.getOwnPropertyDescriptor(
  85  |         HTMLInputElement.prototype,
  86  |         "value",
  87  |       )!.set!;
  88  |       setter.call(input, String(value));
  89  |       input.dispatchEvent(new Event("change", { bubbles: true }));
  90  |     }, Math.floor(durationMs * 0.3));
  91  | 
  92  |     await expect
  93  |       .poll(
  94  |         async () => {
  95  |           const active = page.locator("[data-active='true']");
  96  |           if ((await active.count()) === 0) return -1;
  97  |           const id = await active.first().getAttribute("data-testid");
  98  |           return Number(id?.replace("chunk-", "") ?? -1);
  99  |         },
  100 |         { timeout: 15_000 },
  101 |       )
  102 |       .toBeGreaterThan(0);
  103 | 
  104 |     // --- seek back to 0 ---------------------------------------------------
  105 |     await page.getByTestId("player-scrubber").evaluate((element) => {
  106 |       const input = element as HTMLInputElement;
  107 |       const setter = Object.getOwnPropertyDescriptor(
  108 |         HTMLInputElement.prototype,
  109 |         "value",
  110 |       )!.set!;
  111 |       setter.call(input, "0");
  112 |       input.dispatchEvent(new Event("change", { bubbles: true }));
  113 |     });
  114 | 
  115 |     await expect(page.getByTestId("chunk-0")).toHaveAttribute("data-active", "true", {
  116 |       timeout: 15_000,
  117 |     });
  118 |   });
  119 | 
  120 |   test("playback rate does not shift the highlight (everything is media time)", async ({ page }) => {
  121 |     await login(page);
  122 |     await uploadFixture(page);
  123 |     await expect(page.getByTestId("chunk-0")).toContainText(FIXTURE_MARKER, { timeout: 120_000 });
  124 |     await waitForTerminal(page);
  125 | 
  126 |     await page.getByTestId("player-rate").selectOption("2");
  127 |     await page.getByTestId("play-toggle").click();
  128 | 
  129 |     await expect(page.getByTestId("active-word")).toBeVisible({ timeout: 15_000 });
> 130 |     const first = await page.getByTestId("active-word").innerText();
      |                                                         ^ Error: locator.innerText: Test timeout of 180000ms exceeded.
  131 |     await expect
  132 |       .poll(async () => page.getByTestId("active-word").innerText(), {
  133 |         timeout: 15_000,
  134 |         intervals: [250],
  135 |       })
  136 |       .not.toBe(first);
  137 | 
  138 |     // The word under the highlight must still be a real slice of the active
  139 |     // paragraph's text -- a rate change must not desync the mapping, because
  140 |     // audio.currentTime is media time, not wall-clock time.
  141 |     const active = page.locator("[data-active='true']").first();
  142 |     const paragraph = await active.innerText();
  143 |     const word = await page.getByTestId("active-word").innerText();
  144 |     expect(paragraph).toContain(word);
  145 |   });
  146 | });
  147 | 
```