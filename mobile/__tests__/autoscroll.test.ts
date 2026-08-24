import {
  AUTOSCROLL_BOTTOM_FRACTION,
  AUTOSCROLL_LANDING_FRACTION,
  AUTOSCROLL_MARGIN,
  computeScrollTarget,
  findLineForOffset,
  type TextLine,
} from "@/lib/autoscroll";

function line(text: string, y: number, height = 28): TextLine {
  return { x: 0, y, width: 300, height, text };
}

/** A 400px-tall viewport, matching a typical reading-pane height, scrolled to
 *  `scrollY`. */
function viewport(scrollY: number) {
  return { scrollY, viewportHeight: 400 };
}

describe("computeScrollTarget", () => {
  it("does nothing when the viewport hasn't been measured yet (height 0)", () => {
    // Before the ScrollView's onLayout has fired -- must not treat every
    // word as having crossed the bottom edge of a zero-height viewport.
    const target = computeScrollTarget({ wordTop: 10, wordBottom: 30, scrollY: 0, viewportHeight: 0 });
    expect(target).toBeNull();
  });

  it("does nothing when the word is comfortably inside the viewport", () => {
    const target = computeScrollTarget({
      wordTop: 100,
      wordBottom: 130,
      ...viewport(0),
    });
    expect(target).toBeNull();
  });

  it("does nothing right at the top edge, inside the margin", () => {
    // Sits exactly on the boundary the margin defines -- not yet past it.
    const target = computeScrollTarget({
      wordTop: AUTOSCROLL_MARGIN,
      wordBottom: AUTOSCROLL_MARGIN + 20,
      ...viewport(0),
    });
    expect(target).toBeNull();
  });

  it("does nothing right at the bottom trigger line, inside it", () => {
    const triggerLine = 400 * AUTOSCROLL_BOTTOM_FRACTION;
    const target = computeScrollTarget({
      wordTop: triggerLine - 20,
      wordBottom: triggerLine,
      ...viewport(0),
    });
    expect(target).toBeNull();
  });

  it("scrolls down when the word crosses the bottom trigger line (mid-screen, not the edge)", () => {
    // Viewport [0, 400); bottom trigger sits at 400 * AUTOSCROLL_BOTTOM_FRACTION.
    const triggerLine = 400 * AUTOSCROLL_BOTTOM_FRACTION;
    const target = computeScrollTarget({
      wordTop: triggerLine + 10,
      wordBottom: triggerLine + 40,
      ...viewport(0),
    });
    // Word's bottom should land AUTOSCROLL_LANDING_FRACTION of the way down
    // the viewport, not just inside the bottom edge.
    expect(target).toBe(triggerLine + 40 - 400 * AUTOSCROLL_LANDING_FRACTION);
    expect(target).not.toBeNull();
  });

  it("scrolls up when the word crosses the top edge (e.g. after a chunk swap)", () => {
    // Scrolled to 500; word now sits above the visible area entirely.
    const target = computeScrollTarget({
      wordTop: 50,
      wordBottom: 80,
      ...viewport(500),
    });
    expect(target).toBe(50 - AUTOSCROLL_MARGIN);
  });

  it("never returns a negative offset even for a word near the very top", () => {
    const target = computeScrollTarget({
      wordTop: 5,
      wordBottom: 25,
      ...viewport(0),
    });
    expect(target).toBe(0);
  });

  it("does not overshoot past 0 when scrolling down from near the top", () => {
    const target = computeScrollTarget({
      wordTop: 10,
      wordBottom: 500,
      ...viewport(0),
    });
    // wordBottom - viewportHeight + margin could go negative for a very
    // short scroll distance; clamp to 0.
    expect(target).toBeGreaterThanOrEqual(0);
  });

  it("is a no-op once the returned target is applied (idempotent)", () => {
    const triggerLine = 400 * AUTOSCROLL_BOTTOM_FRACTION;
    const first = computeScrollTarget({
      wordTop: triggerLine + 10,
      wordBottom: triggerLine + 40,
      ...viewport(0),
    });
    expect(first).not.toBeNull();
    const second = computeScrollTarget({
      wordTop: triggerLine + 10,
      wordBottom: triggerLine + 40,
      ...viewport(first as number),
    });
    expect(second).toBeNull();
  });

  it("prioritizes the bottom-trigger case when a word is taller than the viewport", () => {
    // Degenerate case: word spans past both edges. Bottom check runs first,
    // so scrolling follows the word's leading (bottom) edge rather than
    // fighting itself between the two branches.
    const target = computeScrollTarget({
      wordTop: -100,
      wordBottom: 900,
      ...viewport(0),
    });
    expect(target).toBe(Math.max(0, 900 - 400 * AUTOSCROLL_LANDING_FRACTION));
  });
});

describe("findLineForOffset", () => {
  it("returns -1 for an empty lines array", () => {
    expect(findLineForOffset([], 0)).toBe(-1);
  });

  it("returns -1 for a negative offset", () => {
    expect(findLineForOffset([line("Hello world", 0)], -1)).toBe(-1);
  });

  it("finds the line containing an offset in the middle of it", () => {
    const lines = [line("Hello world ", 0), line("this is line two ", 28), line("and line three", 56)];
    // "Hello world " is 12 chars (0-11); "this is line two " starts at 12.
    expect(findLineForOffset(lines, 5)).toBe(0);
    expect(findLineForOffset(lines, 12)).toBe(1);
    expect(findLineForOffset(lines, 15)).toBe(1);
  });

  it("finds an offset exactly at a line boundary as the start of the NEXT line", () => {
    const lines = [line("abc", 0), line("def", 28)];
    // "abc" occupies [0,3); offset 3 is the first char of "def".
    expect(findLineForOffset(lines, 3)).toBe(1);
  });

  it("claims a trailing offset on the last line even past its nominal length", () => {
    // RN's `line.text` can trim trailing whitespace the raw string had, so
    // a highlighted word's offset can land just past every line's nominal
    // range -- the last line should still claim it rather than returning -1.
    const lines = [line("Hello", 0), line("world", 28)];
    expect(findLineForOffset(lines, 10)).toBe(1);
    expect(findLineForOffset(lines, 999)).toBe(1);
  });

  it("finds the correct line across a realistic multi-line paragraph", () => {
    const lines = [
      line("The quick brown fox jumps ", 0),
      line("over the lazy dog and then ", 28),
      line("runs away into the forest.", 56),
    ];
    const fullText = lines.map((l) => l.text).join("");
    const wordStart = fullText.indexOf("runs");
    expect(findLineForOffset(lines, wordStart)).toBe(2);
  });
});
