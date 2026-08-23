import {
  durationDriftExceedsTolerance,
  locateSegment,
  segmentIndexOfChunk,
  segmentsAreContiguous,
  type BookManifest,
  type Segment,
} from "@/lib/manifest";

function segment(i: number, t: number, d: number): Segment {
  return {
    i,
    t,
    d,
    s: i * 1800,
    e: (i + 1) * 1800,
    audioKey: `audio/u/b/${String(i).padStart(6, "0")}.mp3`,
    marksKey: `marks/u/b/${String(i).padStart(6, "0")}.json`,
  };
}

/** `count` contiguous segments of `d` ms each, exactly as the stitcher emits
 *  them (phase-5 §7.2: `segments[k].t === segments[k-1].t + segments[k-1].d`). */
function contiguous(count: number, d = 120_000, indexes?: number[]): Segment[] {
  const out: Segment[] = [];
  let t = 0;
  for (let k = 0; k < count; k += 1) {
    out.push(segment(indexes ? indexes[k] : k, t, d));
    t += d;
  }
  return out;
}

function manifest(segments: Segment[], missing: number[] = []): BookManifest {
  return {
    version: 1,
    bookId: "book-1",
    status: "READY",
    audioKey: "audio/u/b/book.mp3",
    durationMs: segments.reduce((sum, s) => sum + s.d, 0),
    chunksTotal: segments.length + missing.length,
    chunksDone: segments.length + missing.length,
    chunksFailed: missing.length,
    sampleRateHz: 24000,
    segments,
    missing,
  };
}

describe("locateSegment", () => {
  it("returns -1 before the first segment", () => {
    const segments = [segment(0, 5_000, 1_000)];
    expect(locateSegment(segments, 0)).toBe(-1);
    expect(locateSegment(segments, 4_999)).toBe(-1);
  });

  it("selects k, not k-1, at an exact boundary", () => {
    const segments = contiguous(3, 1_000);
    expect(locateSegment(segments, 1_000)).toBe(1);
    expect(locateSegment(segments, 2_000)).toBe(2);
  });

  it("selects the containing segment mid-segment", () => {
    const segments = contiguous(3, 1_000);
    expect(locateSegment(segments, 500)).toBe(0);
    expect(locateSegment(segments, 1_500)).toBe(1);
    expect(locateSegment(segments, 2_999)).toBe(2);
  });

  it("clamps to the last segment past the end", () => {
    // A browser can report currentTime a few ms beyond duration; the
    // highlight should sit on the last word, not vanish.
    const segments = contiguous(3, 1_000);
    expect(locateSegment(segments, 3_000)).toBe(2);
    expect(locateSegment(segments, 99_999)).toBe(2);
  });

  it("handles a single-segment manifest", () => {
    const segments = contiguous(1, 4_200);
    expect(locateSegment(segments, 0)).toBe(0);
    expect(locateSegment(segments, 4_199)).toBe(0);
    expect(locateSegment(segments, 10_000)).toBe(0);
  });

  it("returns -1 for empty segments", () => {
    expect(locateSegment([], 0)).toBe(-1);
    expect(locateSegment([], 12_345)).toBe(-1);
  });

  it("returns a POSITION in segments, not a chunk index, when chunks are missing", () => {
    // Chunks 1 and 3 failed, so they are absent from `segments` entirely
    // (phase-5 Q10) -- the timeline stays contiguous and the two indexings
    // diverge. This is the distinction that would silently light the wrong
    // paragraph if `i` and the array position were ever confused.
    const segments = contiguous(3, 1_000, [0, 2, 4]);

    expect(locateSegment(segments, 1_500)).toBe(1);
    expect(segments[1].i).toBe(2);
    expect(segmentsAreContiguous(manifest(segments, [1, 3]))).toBe(true);
  });

  it("round-trips over a 330-segment sweep", () => {
    const segments = contiguous(330, 4_300);
    for (const s of segments) {
      const position = segments.indexOf(s);
      expect(locateSegment(segments, s.t)).toBe(position);
      expect(locateSegment(segments, s.t + s.d - 1)).toBe(position);
    }
  });
});

describe("segmentsAreContiguous", () => {
  it("accepts a well-formed manifest", () => {
    expect(segmentsAreContiguous(manifest(contiguous(5)))).toBe(true);
  });

  it("rejects a manifest with a gap", () => {
    const segments = contiguous(3, 1_000);
    segments[2] = { ...segments[2], t: segments[2].t + 7 };
    expect(segmentsAreContiguous(manifest(segments))).toBe(false);
  });

  it("rejects out-of-order chunk indexes", () => {
    const segments = contiguous(3, 1_000, [0, 5, 2]);
    expect(segmentsAreContiguous(manifest(segments))).toBe(false);
  });

  it("rejects durations that do not sum to durationMs", () => {
    const base = manifest(contiguous(3, 1_000));
    expect(segmentsAreContiguous({ ...base, durationMs: base.durationMs + 1 })).toBe(false);
  });

  it("accepts an empty (zero-segment) manifest, which is a real state", () => {
    // PARTIAL/NO_AUDIO writes exactly this document, and it is the everyday
    // state of every PR environment.
    expect(segmentsAreContiguous(manifest([], [0, 1, 2]))).toBe(true);
  });
});

describe("segmentIndexOfChunk", () => {
  it("maps a chunk index to its position in segments", () => {
    const segments = contiguous(3, 1_000, [0, 2, 4]);
    expect(segmentIndexOfChunk(segments, 4)).toBe(2);
  });

  it("returns -1 for a chunk with no audio", () => {
    const segments = contiguous(3, 1_000, [0, 2, 4]);
    expect(segmentIndexOfChunk(segments, 1)).toBe(-1);
  });
});

describe("durationDriftExceedsTolerance", () => {
  it("is false for an exact CBR match", () => {
    expect(durationDriftExceedsTolerance(120, 120_000)).toBe(false);
  });

  it("is false just inside 2%", () => {
    expect(durationDriftExceedsTolerance(121.9, 120_000)).toBe(false);
  });

  it("is true beyond 2% (a mixed-engine book with no Xing header)", () => {
    expect(durationDriftExceedsTolerance(130, 120_000)).toBe(true);
  });

  it("is false for a non-finite duration, which is what a fresh element reports", () => {
    expect(durationDriftExceedsTolerance(NaN, 120_000)).toBe(false);
    expect(durationDriftExceedsTolerance(Infinity, 120_000)).toBe(false);
  });

  it("is false when the manifest claims no duration at all", () => {
    expect(durationDriftExceedsTolerance(0, 0)).toBe(false);
  });
});
