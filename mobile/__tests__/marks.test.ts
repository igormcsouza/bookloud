import {
  MAX_CACHED_SEGMENTS,
  MarksCache,
  canSkipSync,
  locateWord,
  rebaseMarks,
  stillInside,
  type MarksEntry,
  type WordMark,
} from "@/lib/marks";

function word(t: number, d: number, s: number, e: number, w = "word"): WordMark {
  return { t, d, s, e, w };
}

/** `count` words at ~400 ms each -- roughly 150 wpm, the pace §6.3's
 *  rAF-vs-timeupdate argument is calibrated against. */
function sentence(count: number): WordMark[] {
  const out: WordMark[] = [];
  let t = 0;
  let s = 0;
  for (let k = 0; k < count; k += 1) {
    const d = 120 + (k % 7) * 60;
    out.push(word(t, d, s, s + 5, `w${k}`));
    t += d;
    s += 6;
  }
  return out;
}

describe("rebaseMarks", () => {
  it("adds segmentStartMs to every t", () => {
    const rebased = rebaseMarks([word(0, 100, 0, 3), word(210, 90, 4, 7)], 118_240);
    expect(rebased.map((m) => m.t)).toEqual([118_240, 118_450]);
  });

  it("leaves s and e UNTOUCHED", () => {
    // phase-4 §7.5's deliberate asymmetry: the per-chunk document indexes
    // into one chunk's text, the manifest indexes into the book. This is the
    // test that catches someone "helpfully" globalizing them -- which would
    // silently break ChunkParagraph's slice, not throw.
    const original = [word(0, 100, 0, 3, "The"), word(210, 90, 4, 7, "cat")];
    const rebased = rebaseMarks(original, 999_999);

    expect(rebased.map((m) => [m.s, m.e])).toEqual([
      [0, 3],
      [4, 7],
    ]);
    expect(rebased.map((m) => m.w)).toEqual(["The", "cat"]);
  });

  it("does not mutate its input", () => {
    const original = [word(0, 100, 0, 3)];
    rebaseMarks(original, 5_000);
    expect(original[0].t).toBe(0);
  });

  it("preserves d", () => {
    expect(rebaseMarks([word(0, 137, 0, 3)], 1_000)[0].d).toBe(137);
  });
});

describe("locateWord", () => {
  it("returns -1 before the first word", () => {
    expect(locateWord([word(500, 100, 0, 3)], 0)).toBe(-1);
    expect(locateWord([word(500, 100, 0, 3)], 499)).toBe(-1);
  });

  it("selects the word at an exact t boundary", () => {
    const words = sentence(5);
    expect(locateWord(words, words[2].t)).toBe(2);
  });

  it("selects the containing word mid-word", () => {
    const words = sentence(5);
    expect(locateWord(words, words[2].t + 10)).toBe(2);
  });

  it("clamps to the last word past the end", () => {
    const words = sentence(5);
    expect(locateWord(words, 9_999_999)).toBe(4);
  });

  it("returns -1 for an empty array", () => {
    expect(locateWord([], 0)).toBe(-1);
  });

  it("resolves a tie to the LAST word sharing that t", () => {
    // build_marks_document only asserts NON-decreasing t, so ties are legal;
    // when two words claim the same millisecond the later one is the one
    // that should be lit.
    const words = [word(0, 50, 0, 3), word(100, 50, 4, 7), word(100, 50, 8, 11)];
    expect(locateWord(words, 100)).toBe(2);
  });

  it("round-trips over a 300-word fixture", () => {
    const words = sentence(300);
    for (let i = 0; i < words.length; i += 1) {
      expect(locateWord(words, words[i].t)).toBe(i);
    }
  });
});

describe("stillInside", () => {
  it("is true across [words[i].t, words[i+1].t) and false at the next boundary", () => {
    const words = sentence(4);
    expect(stillInside(words, 1, words[1].t)).toBe(true);
    expect(stillInside(words, 1, words[2].t - 1)).toBe(true);
    expect(stillInside(words, 1, words[2].t)).toBe(false);
    expect(stillInside(words, 1, words[1].t - 1)).toBe(false);
  });

  it("is true for the last word at any t at or past its start", () => {
    // §6.4: a word is lit by START boundaries, not by [t, t+d). This makes
    // the partition total (no dark gap between words) and moots phase-5
    // §7.2's "engines report word ends optimistically" clamping warning.
    const words = sentence(3);
    const last = words.length - 1;
    expect(stillInside(words, last, words[last].t)).toBe(true);
    expect(stillInside(words, last, words[last].t + words[last].d * 100)).toBe(true);
  });

  it("is false for an out-of-range index", () => {
    const words = sentence(3);
    expect(stillInside(words, -1, 0)).toBe(false);
    expect(stillInside(words, 3, 0)).toBe(false);
    expect(stillInside([], 0, 0)).toBe(false);
  });

  it("agrees with locateWord across 1000 sampled times", () => {
    // The fast-path/slow-path consistency property, stated as a decision in
    // Q8 and asserted here: the rAF loop trusts `stillInside` for ~59 of
    // every 60 frames and only falls back to the bisect on a discontinuity.
    // If the two could ever disagree, the highlight would stick.
    const words = sentence(300);
    const end = words[words.length - 1].t + 500;
    for (let k = 0; k < 1000; k += 1) {
      const t = Math.floor((k / 1000) * end);
      const located = locateWord(words, t);
      for (let i = 0; i < words.length; i += 1) {
        expect(stillInside(words, i, t)).toBe(i === located);
      }
    }
  });
});

describe("canSkipSync", () => {
  // Issue #13 regression: reading froze on a chunk's last word forever,
  // while the audio itself kept advancing. Root cause was `usePlayback`'s
  // sync loop trusting `stillInside` alone as its "nothing changed, skip
  // the resync" fast path -- `stillInside` is true for the last word at ANY
  // t at or past its start (tested above), which is correct for lighting
  // that word but wrongly told the loop it never needed to look at the next
  // segment again. `canSkipSync` adds the segment-end bound that was
  // missing.
  const words = sentence(4);
  const last = words.length - 1;
  const segmentEndMs = words[last].t + words[last].d * 2; // some time after the last word starts

  it("still allows the fast path mid-word, same as stillInside alone", () => {
    expect(canSkipSync(words, 1, words[1].t, segmentEndMs)).toBe(true);
  });

  it("REGRESSION: false once tMs reaches the segment end, even parked on the last word", () => {
    // This is exactly the stuck state from issue #13: wordRef parked on the
    // last word (stillInside would say "yes, skip" forever), but playback
    // has moved on to the next chunk's audio.
    expect(stillInside(words, last, segmentEndMs)).toBe(true); // the old, buggy signal
    expect(canSkipSync(words, last, segmentEndMs, segmentEndMs)).toBe(false); // the fix
  });

  it("REGRESSION: false arbitrarily far past the segment end, not just at the boundary", () => {
    expect(canSkipSync(words, last, segmentEndMs + 10 * 60_000, segmentEndMs)).toBe(false);
  });

  it("still true right up to (but not at) the segment end", () => {
    expect(canSkipSync(words, last, segmentEndMs - 1, segmentEndMs)).toBe(true);
  });

  it("is false with no cached words (forces a resync to fetch them)", () => {
    expect(canSkipSync(undefined, 0, 0, segmentEndMs)).toBe(false);
  });

  it("is false with no current segment (e.g. before the first segment)", () => {
    expect(canSkipSync(words, 0, 0, undefined)).toBe(false);
  });
});

describe("MarksCache", () => {
  const load404 = () => Promise.resolve<MarksEntry>(null);

  it("dedupes concurrent calls for the same index into one fetch", () => {
    const loader = jest.fn(() => Promise.resolve([word(0, 100, 0, 3)]));
    const cache = new MarksCache(loader);

    const a = cache.ensure(7, 1_000);
    const b = cache.ensure(7, 1_000);

    expect(loader).toHaveBeenCalledTimes(1);
    return Promise.all([a, b]).then(([first, second]) => {
      expect(first).toEqual(second);
      expect(first?.[0].t).toBe(1_000);
    });
  });

  it("rebases on insert, so the hot path does zero arithmetic", async () => {
    const cache = new MarksCache(() => Promise.resolve([word(0, 100, 0, 3), word(210, 90, 4, 7)]));

    await cache.ensure(1, 118_240);

    expect(cache.get(1)?.map((m) => m.t)).toEqual([118_240, 118_450]);
  });

  it("stores null for a 404 and NEVER re-requests it", async () => {
    // The nastiest failure mode in the design: without the negative cache the
    // rAF loop re-requests a missing marks object every frame -- 60 requests
    // per second against ~5 executions of API headroom, from a UI that just
    // looks stuck.
    const loader = jest.fn(load404);
    const cache = new MarksCache(loader);

    expect(await cache.ensure(3, 0)).toBeNull();
    expect(cache.get(3)).toBeNull();
    expect(cache.has(3)).toBe(true);

    for (let k = 0; k < 120; k += 1) await cache.ensure(3, 0);

    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("returns undefined (not null) for an index never loaded", () => {
    const cache = new MarksCache(load404);
    expect(cache.get(9)).toBeUndefined();
    expect(cache.has(9)).toBe(false);
  });

  it("does not negatively cache a network failure", async () => {
    // Unlike a 404, a rejected fetch is not a statement about the object.
    const loader = jest
      .fn<Promise<MarksEntry>, [number]>()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce([word(0, 10, 0, 3)]);
    const cache = new MarksCache(loader);

    expect(await cache.ensure(2, 0)).toBeNull();
    expect(cache.has(2)).toBe(false);

    expect(await cache.ensure(2, 0)).not.toBeNull();
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("evict keeps a bounded window and always keeps the centre", async () => {
    const cache = new MarksCache(() => Promise.resolve([word(0, 10, 0, 3)]));
    for (let k = 0; k < 30; k += 1) await cache.ensure(k, k * 1_000);

    cache.evict(20);

    expect(cache.size).toBeLessThanOrEqual(MAX_CACHED_SEGMENTS);
    expect(cache.has(20)).toBe(true);
    expect(cache.has(16)).toBe(true);
    expect(cache.has(24)).toBe(true);
    expect(cache.has(15)).toBe(false);
    expect(cache.has(25)).toBe(false);
  });

  it("evict honours a custom radius", async () => {
    const cache = new MarksCache(() => Promise.resolve([word(0, 10, 0, 3)]));
    for (let k = 0; k < 10; k += 1) await cache.ensure(k, 0);

    cache.evict(5, 1);

    expect(Array.from({ length: 10 }, (_, k) => cache.has(k))).toEqual([
      false, false, false, false, true, true, true, false, false, false,
    ]);
  });
});
