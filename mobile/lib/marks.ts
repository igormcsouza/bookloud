// Per-chunk word timings: the second binary search, the rebasing rule, and
// the bounded cache that keeps the rAF loop from becoming a request storm.

export type WordMark = {
  /** Start, ms. Chunk-relative as stored; **book-global after
   *  `rebaseMarks`** -- which is the only mutation this module makes. */
  t: number;
  /** Duration, ms. Kept in the type (a "progress within the word" effect
   *  would want it) and deliberately **unused for highlighting**, see
   *  `stillInside`. */
  d: number;
  /** Character offsets into THIS CHUNK's text. Left chunk-relative on
   *  purpose (phase-4 §7.5's deliberate asymmetry: the per-chunk document
   *  indexes into one chunk, the manifest indexes into the book). It is
   *  exactly what lets `ChunkParagraph` slice the active paragraph with no
   *  global-offset bookkeeping at all. */
  s: number;
  e: number;
  w: string;
};

export type MarksDocument = {
  version: number;
  bookId: string;
  chunkIndex: number;
  source: string;
  voice: string;
  /** "measured" (edge-tts's real WordBoundary events) or "estimated"
   *  (Google's per-sentence interpolation, and SilentSynthesizer). Estimated
   *  marks drift visibly inside a long sentence -- a real artefact that would
   *  look like a bug, so the UI renders them with a lower-opacity mark and a
   *  tooltip rather than pretending they are exact (OQ-8). */
  timing: "measured" | "estimated";
  audioKey: string;
  charStart: number;
  charEnd: number;
  durationMs: number;
  wordCount: number;
  words: WordMark[];
};

/** `WordMark[]` when loaded, `null` when **known absent** (a 404).
 *
 *  The `null` is not a convenience. A chunk that is `DONE` but whose marks
 *  object is missing -- or, far more commonly, a chunk that FAILED, which is
 *  every chunk in every PR environment -- returns 404. Without a negative
 *  cache the rAF loop re-requests it on the very next frame: 60 requests per
 *  second against an API Lambda with ~5 executions of headroom, from a UI
 *  that merely looks stuck. */
export type MarksEntry = WordMark[] | null;

/**
 * `t_global = segment.t + word.t`, applied **once at cache insert**, so the
 * per-frame path does zero arithmetic.
 *
 * `s`/`e` are left CHUNK-relative on purpose (phase-4 §7.5). This is the
 * function someone will eventually try to "fix" by globalizing them; the
 * test that catches it is `marks.test.ts` case 1.
 */
export function rebaseMarks(words: WordMark[], segmentStartMs: number): WordMark[] {
  return words.map((word) => ({ ...word, t: word.t + segmentStartMs }));
}

/**
 * `bisectRight(words, tMs, by .t) - 1`; `-1` before the first word and for an
 * empty array.
 *
 * Ties resolve to the **last** word sharing that `t`: the marks document only
 * asserts NON-decreasing `t`, and when two words claim the same millisecond
 * the later one is the one that should be lit.
 */
export function locateWord(words: WordMark[], tMs: number): number {
  if (words.length === 0) return -1;
  if (tMs < words[0].t) return -1;

  let lo = 0;
  let hi = words.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (words[mid].t <= tMs) lo = mid + 1;
    else hi = mid;
  }
  return lo - 1;
}

/**
 * The rAF hot path's O(1) fast path: true while `tMs` is still inside word
 * `i`'s span.
 *
 * **A word is lit from `words[i].t` until `words[i+1].t`** -- by *start*
 * boundaries, not by `[t, t+d)`. Three consequences, all deliberate:
 *
 * - The partition is total, so there is never a frame with no word lit and
 *   the highlight never flickers off between words.
 * - This is exactly the inverse of `locateWord`, so the fast path and the
 *   slow path cannot disagree. (`marks.test.ts` asserts that as a property
 *   over 1,000 sampled times.)
 * - Phase-5 §7.2's warning -- "it must clamp; the last word's `t + d` can
 *   exceed the segment duration by a few ms because engines report word ends
 *   optimistically" -- becomes moot, because `d` is never consulted.
 */
export function stillInside(words: WordMark[], i: number, tMs: number): boolean {
  if (i < 0 || i >= words.length) return false;
  if (tMs < words[i].t) return false;
  return i + 1 >= words.length || tMs < words[i + 1].t;
}

/** Default radius. `evict` drops entries *further than* `radius` from the
 *  centre, so it holds at most `2 * radius + 1` == **9** entries: the centre
 *  plus four either side. (PLANS/phase-6.md §6.2 glosses this as "≤8", which
 *  no symmetric rule can produce for radius 4; the prose definition wins, and
 *  the one-entry difference changes nothing about the point.) A 330-segment
 *  book fully traversed would otherwise hold ~100k word objects (~15 MB of JS
 *  heap); this holds ~2,700. */
export const DEFAULT_EVICT_RADIUS = 4;
export const MAX_CACHED_SEGMENTS = 2 * DEFAULT_EVICT_RADIUS + 1;

type Loader = (segmentIndex: number) => Promise<MarksEntry>;

/**
 * Lazily loaded, rebased word marks for the segments around the playhead.
 *
 * Rejected alternatives, both for the same arithmetic reason phase-5 §7.2
 * used to reject a merged word array, applied one level down:
 * - **All up front**: 330 files x ~25 KB is ~6.6 MB across 330 requests
 *   before the first note plays.
 * - **On demand with no prefetch**: the fetch lands in the rAF path at a
 *   chunk boundary, blanking the highlight for a round trip every ~2 minutes.
 */
export class MarksCache {
  private readonly entries = new Map<number, MarksEntry>();
  private readonly inflight = new Map<number, Promise<MarksEntry>>();

  constructor(private readonly load: Loader) {}

  /** Loaded, rebased marks for segment `i`; `null` if known absent;
   *  `undefined` if not loaded. Never triggers a fetch -- this is what the
   *  60 Hz path calls. */
  get(i: number): MarksEntry | undefined {
    return this.entries.get(i);
  }

  has(i: number): boolean {
    return this.entries.has(i);
  }

  /** Idempotent: concurrent calls for the same `i` share one in-flight
   *  promise, and a resolved entry (including a negatively-cached `null`) is
   *  never re-requested. */
  ensure(i: number, segmentStartMs: number): Promise<MarksEntry> {
    const cached = this.entries.get(i);
    if (cached !== undefined) return Promise.resolve(cached);

    const pending = this.inflight.get(i);
    if (pending) return pending;

    const promise = this.load(i)
      .then((words) => {
        const entry: MarksEntry = words === null ? null : rebaseMarks(words, segmentStartMs);
        this.entries.set(i, entry);
        return entry;
      })
      .catch(() => {
        // A network failure is NOT negatively cached -- unlike a 404, it is
        // not a statement about the object. Leaving it uncached lets the next
        // segment entry retry; leaving it out of `entries` is what stops the
        // rAF loop retrying it every frame, because the loop only ever calls
        // `ensure` on a segment *change*.
        return null;
      })
      .finally(() => {
        this.inflight.delete(i);
      });

    this.inflight.set(i, promise);
    return promise;
  }

  /** Drops entries further than `radius` from `centre`. `centre` itself is
   *  always kept, so evicting can never blank the segment being played. */
  evict(centre: number, radius: number = DEFAULT_EVICT_RADIUS): void {
    for (const key of Array.from(this.entries.keys())) {
      if (Math.abs(key - centre) > radius) this.entries.delete(key);
    }
  }

  get size(): number {
    return this.entries.size;
  }
}
