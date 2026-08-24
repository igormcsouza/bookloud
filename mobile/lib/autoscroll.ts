// Pure geometry for issue #15's auto-scroll fix: once the highlighted word
// scrolls out of the visible viewport, the reading pane should follow it --
// smoothly, and without fighting the user's own scrolling by re-centering on
// every single word tick while the word is already comfortably on screen.

export type ScrollTarget = {
  /** The highlighted word's top edge, in content coordinates (i.e. relative
   *  to the scrollable content, not the viewport). */
  wordTop: number;
  /** The highlighted word's bottom edge, in the same coordinate space. */
  wordBottom: number;
  /** The ScrollView's current `contentOffset.y`. */
  scrollY: number;
  /** The ScrollView's visible height (its own layout height, not its
   *  content height). */
  viewportHeight: number;
};

/** Padding (px) kept between the highlighted word and the viewport's TOP edge
 *  when auto-scrolling up -- lands the word comfortably below the top edge
 *  instead of flush against it, which reads as jarring and can clip
 *  ascenders against the viewport edge. */
export const AUTOSCROLL_MARGIN = 24;

/** How far down the viewport (as a fraction of its height) the bottom
 *  trigger line sits. Reading eyesight rests around the middle of the
 *  screen, not the bottom edge -- keeping the highlight comfortably above
 *  that natural gaze line (rather than letting it drift down to just above
 *  the audio controls) is what keeps the follow feeling anticipatory
 *  instead of catching up. */
export const AUTOSCROLL_BOTTOM_FRACTION = 0.45;

/** Where a word that trips the bottom trigger is scrolled back TO, again as
 *  a fraction of viewport height from the top -- comfortably above
 *  `AUTOSCROLL_BOTTOM_FRACTION` so the same word doesn't immediately retrip
 *  the trigger on the very next tick. */
export const AUTOSCROLL_LANDING_FRACTION = 0.28;

/**
 * The new `contentOffset.y` to scroll to so the highlighted word sits back
 * around `AUTOSCROLL_LANDING_FRACTION` of the way down the viewport, or
 * `null` if the word is already sufficiently visible (above the
 * `AUTOSCROLL_BOTTOM_FRACTION` trigger line, at or below the top margin) and
 * no scroll is needed.
 *
 * Deliberately asymmetric in *why* it scrolls, not just *where* to: scrolling
 * only fires when the word is actually near/past a trigger, so playback
 * advancing word-by-word inside an already-visible line never triggers a
 * scroll (the "don't scroll on every tick" requirement) -- `stillInside`'s
 * sibling concern, one layer up in the UI instead of the sync loop.
 */
export function computeScrollTarget(target: ScrollTarget): number | null {
  const { wordTop, wordBottom, scrollY, viewportHeight } = target;
  // Not measured yet (first render, before the ScrollView's onLayout has
  // fired) -- nothing to scroll relative to, so do nothing rather than
  // treat every word as "past the bottom edge" of a zero-height viewport.
  if (viewportHeight <= 0) return null;
  const visibleTop = scrollY;
  const bottomTrigger = visibleTop + viewportHeight * AUTOSCROLL_BOTTOM_FRACTION;

  // Past the bottom trigger line (mid-screen, not the bottom edge): bring
  // the word up to `AUTOSCROLL_LANDING_FRACTION` of the way down the
  // viewport instead of just barely back inside the fold.
  if (wordBottom > bottomTrigger) {
    return Math.max(0, wordBottom - viewportHeight * AUTOSCROLL_LANDING_FRACTION);
  }

  // Above the fold (or crossing the top edge): bring the word to
  // `AUTOSCROLL_MARGIN` below the top edge.
  if (wordTop < visibleTop + AUTOSCROLL_MARGIN) {
    return Math.max(0, wordTop - AUTOSCROLL_MARGIN);
  }

  return null;
}

/** One line, as reported by React Native's `Text` `onTextLayout` event
 *  (`nativeEvent.lines`): `x`/`y`/`width`/`height` in the parent `Text`'s own
 *  coordinate space, plus the substring it renders. */
export type TextLine = {
  x: number;
  y: number;
  width: number;
  height: number;
  text: string;
};

/**
 * Which line (by index into `lines`) contains character offset `charIndex`
 * of the full rendered string, or `-1` if `lines` is empty or `charIndex` is
 * out of range.
 *
 * This exists because a nested inline `<Text>` span is NOT a reliable
 * `onLayout` target (RN's text-flattening optimization frequently merges it
 * into its parent's native text node, so `onLayout` may never fire on it,
 * and when it does the coordinate space isn't documented or guaranteed --
 * see the file header). `onTextLayout` on the *outer* Text, by contrast, is
 * RN's documented, cross-platform API for exactly this: it reports each
 * rendered line's own box plus the text it contains, so the highlighted
 * word's line -- and so its `y`/`height` for `computeScrollTarget` -- can be
 * found by walking `lines` and summing each line's text length until
 * `charIndex` falls inside one.
 */
export function findLineForOffset(lines: TextLine[], charIndex: number): number {
  if (charIndex < 0) return -1;
  let consumed = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const lineLength = lines[i].text.length;
    // Each line's slice of the full string is `[consumed, consumed + lineLength)`,
    // except the trailing line, which should also claim any remainder (e.g. a
    // final space RN trims from `line.text`) -- otherwise a highlighted word
    // right at the very end of the chunk would fall just past every line's
    // range and match nothing.
    const isLast = i === lines.length - 1;
    const end = consumed + lineLength;
    if (charIndex < end || (isLast && charIndex >= consumed)) return i;
    consumed = end;
  }
  return -1;
}
