import { act, cleanup, render, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useReadingAnchor } from "@/hooks/useReadingAnchor";

class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  callback: (entries: unknown[], observer: unknown) => void;

  constructor(callback: (entries: unknown[], observer: unknown) => void) {
    this.callback = callback;
    FakeIntersectionObserver.instances.push(this);
  }

  observe() {}
  unobserve() {}
  disconnect() {}

  trigger(entries: Array<{ isIntersecting: boolean; target: Element; boundingClientRect: { top: number } }>) {
    this.callback(entries, this);
  }
}

beforeEach(() => {
  FakeIntersectionObserver.instances = [];
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver as unknown as typeof IntersectionObserver);
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("useReadingAnchor", () => {
  it("playback chunkIndex >= 0 wins", () => {
    const { result } = renderHook(() => useReadingAnchor(5, 10));
    expect(result.current.current).toBe(5);
  });

  it("clamps a playback chunkIndex to chunkCount - 1", () => {
    const { result } = renderHook(() => useReadingAnchor(99, 10));
    expect(result.current.current).toBe(9);
  });

  it("with chunkIndex === -1, the topmost intersecting data-chunk-index wins", () => {
    document.body.innerHTML =
      '<div data-chunk-index="3"></div><div data-chunk-index="7"></div>';
    const { result } = renderHook(() => useReadingAnchor(-1, 10));
    const observer = FakeIntersectionObserver.instances[0];
    const el3 = document.querySelector('[data-chunk-index="3"]') as Element;
    const el7 = document.querySelector('[data-chunk-index="7"]') as Element;

    act(() => {
      observer.trigger([
        { isIntersecting: true, target: el7, boundingClientRect: { top: 200 } },
        { isIntersecting: true, target: el3, boundingClientRect: { top: 10 } },
      ]);
    });

    expect(result.current.current).toBe(3);
  });

  it("defaults to 0 with neither a playback index nor an intersecting element", () => {
    const { result } = renderHook(() => useReadingAnchor(-1, 10));
    expect(result.current.current).toBe(0);
  });

  it("scrolling (an IntersectionObserver callback) does not re-render the consumer", () => {
    document.body.innerHTML = '<div data-chunk-index="0"></div>';
    let renders = 0;

    function Host() {
      renders += 1;
      useReadingAnchor(-1, 10);
      return null;
    }

    render(<Host />);
    const rendersAfterMount = renders;
    const observer = FakeIntersectionObserver.instances[0];
    const el = document.querySelector('[data-chunk-index="0"]') as Element;

    act(() => {
      observer.trigger([{ isIntersecting: true, target: el, boundingClientRect: { top: 5 } }]);
    });

    expect(renders).toBe(rendersAfterMount);
  });
});
