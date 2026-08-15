import { describe, expect, it } from "vitest";
import { createSseParser } from "@/lib/chat";

describe("createSseParser", () => {
  it("parses one complete frame delivered in one chunk", () => {
    const parse = createSseParser();
    const events = parse('event: meta\ndata: {"model":"stub","enabled":false,"reason":"NON_PROD","anchorChunk":0,"windowChunks":[0],"messageId":"a1"}\n\n');
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ type: "meta", model: "stub", anchorChunk: 0 });
  });

  it("parses one frame split across three chunks, mid-`data:` prefix and mid-JSON", () => {
    const parse = createSseParser();
    const full = 'event: delta\ndata: {"text":"hello world"}\n\n';
    const a = full.slice(0, 8); // splits mid "event: d|elta"
    const b = full.slice(8, 24); // splits mid "data: {\"te|xt\":..."
    const c = full.slice(24);

    expect(parse(a)).toHaveLength(0);
    expect(parse(b)).toHaveLength(0);
    const events = parse(c);
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({ type: "delta", text: "hello world" });
  });

  it("parses three complete frames delivered in one chunk, in order", () => {
    const parse = createSseParser();
    const chunk =
      'event: delta\ndata: {"text":"a"}\n\n' +
      'event: delta\ndata: {"text":"b"}\n\n' +
      'event: delta\ndata: {"text":"c"}\n\n';
    const events = parse(chunk);
    expect(events.map((e) => (e as { text: string }).text)).toEqual(["a", "b", "c"]);
  });

  it("handles \\r\\n line endings", () => {
    const parse = createSseParser();
    const events = parse('event: delta\r\ndata: {"text":"hi"}\r\n\r\n');
    expect(events).toEqual([{ type: "delta", text: "hi" }]);
  });

  it("skips `:` keepalive comment lines without emitting", () => {
    const parse = createSseParser();
    const events = parse(': keepalive\n\nevent: delta\ndata: {"text":"after"}\n\n');
    expect(events).toEqual([{ type: "delta", text: "after" }]);
  });

  it("ignores an unknown event type, and a following known frame still parses", () => {
    const parse = createSseParser();
    const events = parse(
      'event: ping\ndata: {"whatever":true}\n\n' + 'event: delta\ndata: {"text":"ok"}\n\n',
    );
    expect(events).toEqual([{ type: "delta", text: "ok" }]);
  });

  it("skips a data line that is not valid JSON and keeps working", () => {
    const parse = createSseParser();
    const events = parse(
      'event: delta\ndata: {not json}\n\n' + 'event: delta\ndata: {"text":"still works"}\n\n',
    );
    expect(events).toEqual([{ type: "delta", text: "still works" }]);
  });

  it("round-trips a disabled meta frame's reason and windowChunks", () => {
    const parse = createSseParser();
    const events = parse(
      'event: meta\ndata: {"model":"stub","enabled":false,"reason":"NON_PROD","anchorChunk":12,"windowChunks":[10,11,12,13,14],"messageId":"m1"}\n\n',
    );
    expect(events).toEqual([
      {
        type: "meta",
        model: "stub",
        enabled: false,
        reason: "NON_PROD",
        anchorChunk: 12,
        windowChunks: [10, 11, 12, 13, 14],
        messageId: "m1",
      },
    ]);
  });
});
