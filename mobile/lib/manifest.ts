// The book manifest (`marks/<u>/<b>/book.json`) and the first of the two
// binary searches the highlight rests on.
//
// PLANS/phase-5.md §1 invariant 3, restated because everything here depends
// on it: **the manifest is the timeline; the audio file is an optimization.**
// Position, segment and word are all derived from the manifest plus the
// per-chunk marks -- never from `audio.duration`, which for a mixed-engine
// book is a browser *estimate* (the stitcher writes no Xing header, phase-5
// §7.1).

/** One `DONE` chunk's slice of the book timeline. Short keys are the stored
 *  shape, not an abbreviation invented here: a 330-segment document stays
 *  ~60 KB and still readable in the S3 console (phase-5 §7.2). */
export type Segment = {
  /** Chunk index. NOT a position in `segments` -- failed chunks are absent
   *  from the array entirely, so the two diverge as soon as anything fails. */
  i: number;
  /** Book-global start, ms. */
  t: number;
  /** Duration, ms. */
  d: number;
  /** Book-global character offsets. Deliberately asymmetric with the
   *  per-chunk marks, whose `s`/`e` are chunk-relative (phase-4 §7.5). */
  s: number;
  e: number;
  audioKey: string;
  marksKey: string | null;
  /** Byte range into `book.mp3`. Unused this phase (§3.3): `currentTime =
   *  ms/1000` is one line and correct for a uniform CBR book. Kept because
   *  it is the escape hatch if the duration-drift warning ever fires. */
  b0?: number;
  b1?: number;
};

export type BookManifest = {
  version: number;
  bookId: string;
  status: string;
  /** `null` when nothing was concatenated -- the everyday state of every
   *  environment except local compose (PLANS/phase-4.md §0). */
  audioKey: string | null;
  durationMs: number;
  chunksTotal: number;
  chunksDone: number;
  chunksFailed: number;
  sampleRateHz: number | null;
  segments: Segment[];
  /** Chunk indexes with no audio. These are ABSENT from `segments` rather
   *  than represented as silence (phase-5 Q10), which is what makes the
   *  timeline contiguous and `locateSegment` unable to land "in a gap". */
  missing: number[];
};

/**
 * Index into `segments` of the segment covering `tMs`; `-1` before the first
 * (and for an empty manifest).
 *
 * `bisectRight(segments, tMs, by .t) - 1`, clamped to the last segment past
 * the end -- a browser can report `currentTime` a few ms beyond `duration`,
 * and the highlight should sit on the last word rather than vanish.
 *
 * Segments partition the timeline contiguously (phase-5 §7.2:
 * `segments[k].t === segments[k-1].t + segments[k-1].d`, exactly), and
 * `missing` chunks are absent from `segments` rather than rendered as
 * silence, so this can never land inside a gap.
 *
 * **The returned index is a position in `segments`, NOT a chunk index.** For
 * the chunk, read `segments[result].i`.
 */
export function locateSegment(segments: Segment[], tMs: number): number {
  if (segments.length === 0) return -1;
  if (tMs < segments[0].t) return -1;

  let lo = 0;
  let hi = segments.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (segments[mid].t <= tMs) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

/**
 * The dev-mode assertion behind `locateSegment`'s precondition. Returns
 * false for a manifest whose segments are out of order, overlapping, or
 * separated by a gap, or whose durations don't sum to `durationMs`.
 *
 * Worth having as a function rather than a comment: a corrupt manifest does
 * not crash anything -- it silently desyncs the highlight, which reads as
 * "the highlighting is a bit off" rather than as a data bug.
 */
export function segmentsAreContiguous(manifest: BookManifest): boolean {
  let expected = 0;
  let previousIndex = -1;
  for (const segment of manifest.segments) {
    if (segment.i <= previousIndex) return false;
    if (segment.t !== expected) return false;
    previousIndex = segment.i;
    expected = segment.t + segment.d;
  }
  return expected === manifest.durationMs;
}

/** The manifest position of a chunk index, or -1 when that chunk has no
 *  audio. Used by "read from here" clicks, which know a chunk and need a
 *  time. */
export function segmentIndexOfChunk(segments: Segment[], chunkIndex: number): number {
  return segments.findIndex((segment) => segment.i === chunkIndex);
}

/** §3.3: a mixed-engine book's `audio.duration` is an estimate. More than 2%
 *  off the manifest means seeking will be visibly imprecise -- a named,
 *  detectable failure mode rather than a mystery. */
export const DURATION_DRIFT_TOLERANCE = 0.02;

export function durationDriftExceedsTolerance(
  audioDurationSeconds: number,
  manifestDurationMs: number,
): boolean {
  if (!Number.isFinite(audioDurationSeconds) || manifestDurationMs <= 0) return false;
  const drift = Math.abs(audioDurationSeconds * 1000 - manifestDurationMs) / manifestDurationMs;
  return drift > DURATION_DRIFT_TOLERANCE;
}
