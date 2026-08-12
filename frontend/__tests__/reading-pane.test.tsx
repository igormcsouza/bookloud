import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ReadingPane from "@/components/ReadingPane";
import ChunkParagraph from "@/components/ChunkParagraph";
import type { Chunk } from "@/lib/books";

function chunk(index: number, text: string): Chunk {
  return {
    index,
    text,
    charStart: index * 100,
    charEnd: (index + 1) * 100,
    audioKey: null,
    marksKey: null,
    status: "DONE",
    pageStart: 1,
    pageEnd: 1,
    durationMs: 1000,
    failureReason: null,
    synthesisSource: "silent",
  };
}

const CHUNKS = [chunk(0, "The cat sat on the mat."), chunk(1, "It was a fine day."), chunk(2, "The end.")];

afterEach(cleanup);

describe("ReadingPane", () => {
  it("renders every chunk's text", () => {
    render(<ReadingPane chunks={CHUNKS} activeChunkIndex={-1} wordStart={-1} wordEnd={-1} missingChunks={[]} />);

    expect(screen.getByText(/The cat sat on the mat/)).toBeInTheDocument();
    expect(screen.getByText(/It was a fine day/)).toBeInTheDocument();
    expect(screen.getByText(/The end/)).toBeInTheDocument();
  });

  it("renders exactly one active-word mark, whose text is the chunk-relative slice", () => {
    // Chunk-relative offsets, straight from the marks document -- no global
    // bookkeeping (phase-4 §7.5's asymmetry is what buys this).
    render(<ReadingPane chunks={CHUNKS} activeChunkIndex={0} wordStart={4} wordEnd={7} missingChunks={[]} />);

    const marks = screen.getAllByTestId("active-word");
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveTextContent("cat");
    expect(marks[0].textContent).toBe(CHUNKS[0].text.slice(4, 7));
  });

  it("renders no mark and a chunk-level tint when wordStart is -1", () => {
    // The one-round-trip gap while the segment's marks load. "The highlight
    // vanished" would otherwise read as a bug (§6.4).
    render(<ReadingPane chunks={CHUNKS} activeChunkIndex={1} wordStart={-1} wordEnd={-1} missingChunks={[]} />);

    expect(screen.queryByTestId("active-word")).toBeNull();
    expect(screen.getByTestId("chunk-1")).toHaveAttribute("data-active", "true");
  });

  it("renders a missing chunk's muted marker and keeps it readable", () => {
    // §9 rule 1: if chunk text exists, it renders. Always.
    render(<ReadingPane chunks={CHUNKS} activeChunkIndex={-1} wordStart={-1} wordEnd={-1} missingChunks={[1]} />);

    expect(screen.getByTestId("missing-marker-1")).toBeInTheDocument();
    expect(screen.getByTestId("chunk-1")).toHaveAttribute("data-missing", "true");
    expect(screen.getByText(/It was a fine day/)).toBeInTheDocument();
    expect(screen.queryByTestId("missing-marker-0")).toBeNull();
  });

  it("marks an estimated-timing highlight differently from a measured one", () => {
    // OQ-8: estimated marks drift visibly inside a long sentence -- a real
    // artefact that would look like a bug without a visual tell.
    const { rerender } = render(
      <ReadingPane chunks={CHUNKS} activeChunkIndex={0} wordStart={4} wordEnd={7} missingChunks={[]} />,
    );
    expect(screen.getByTestId("active-word")).toHaveAttribute("data-timing", "measured");

    rerender(
      <ReadingPane
        chunks={CHUNKS}
        activeChunkIndex={0}
        wordStart={4}
        wordEnd={7}
        missingChunks={[]}
        estimatedTiming
      />,
    );
    expect(screen.getByTestId("active-word")).toHaveAttribute("data-timing", "estimated");
    expect(screen.getByTestId("active-word")).toHaveAttribute("title", "Approximate timing");
  });

  it("renders a skeleton when no chunks have arrived yet", () => {
    render(<ReadingPane chunks={[]} activeChunkIndex={-1} wordStart={-1} wordEnd={-1} missingChunks={[]} />);
    expect(screen.getByTestId("reading-pane-skeleton")).toBeInTheDocument();
  });

  it("has a follow-along toggle, on by default", () => {
    render(<ReadingPane chunks={CHUNKS} activeChunkIndex={0} wordStart={-1} wordEnd={-1} missingChunks={[]} />);
    expect(screen.getByTestId("follow-along")).toBeChecked();
  });
});

describe("ChunkParagraph memoization", () => {
  it("does NOT re-render inactive paragraphs when the active word changes", () => {
    // The React.memo guarantee, asserted rather than trusted: without it a
    // word change (~2-3 Hz) would re-render all 330 paragraphs.
    //
    // The render counter is a getter on `chunk.text`, which ChunkParagraph
    // reads on every render and nowhere else -- a real count, not a proxy for
    // one. (Comparing DOM node identity would not work: React reuses nodes
    // across re-renders whether or not memo bailed out.)
    const reads = new Map<number, number>();
    const counting = CHUNKS.map((c) =>
      Object.defineProperty({ ...c }, "text", {
        get() {
          reads.set(c.index, (reads.get(c.index) ?? 0) + 1);
          return c.text;
        },
      }),
    ) as Chunk[];

    function Pane({ wordStart, wordEnd }: { wordStart: number; wordEnd: number }) {
      return (
        <>
          {counting.map((c) => {
            const active = c.index === 0;
            return (
              <ChunkParagraph
                key={c.index}
                chunk={c}
                active={active}
                missing={false}
                wordStart={active ? wordStart : -1}
                wordEnd={active ? wordEnd : -1}
              />
            );
          })}
        </>
      );
    }

    const { rerender } = render(<Pane wordStart={0} wordEnd={3} />);
    const inactiveAfterFirst = [reads.get(1) ?? 0, reads.get(2) ?? 0];

    rerender(<Pane wordStart={4} wordEnd={7} />);
    rerender(<Pane wordStart={8} wordEnd={11} />);

    // The active paragraph re-rendered (its <mark> moved)...
    expect(screen.getByTestId("active-word")).toHaveTextContent("sat");
    // ...and the other two did not read their text again at all.
    expect([reads.get(1) ?? 0, reads.get(2) ?? 0]).toEqual(inactiveAfterFirst);
  });
});
