import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ChatTurn from "@/components/ChatTurn";

afterEach(cleanup);

describe("ChatTurn streaming indicator", () => {
  it("shows a three-dot 'thinking' indicator while streaming with no content yet", () => {
    render(
      <ChatTurn id="streaming" role="assistant" content="" finishReason={null} isStreaming />,
    );
    expect(screen.getByRole("status", { name: "Thinking" })).toBeInTheDocument();
  });

  it("shows the blinking cursor, not the dots, once content has started arriving", () => {
    render(
      <ChatTurn id="streaming" role="assistant" content="Partial answer" finishReason={null} isStreaming />,
    );
    expect(screen.queryByRole("status", { name: "Thinking" })).toBeNull();
    expect(screen.getByText("Partial answer")).toBeInTheDocument();
  });

  it("shows neither dots nor cursor once streaming has finished", () => {
    render(
      <ChatTurn id="1" role="assistant" content="Final answer" finishReason={null} isStreaming={false} />,
    );
    expect(screen.queryByRole("status", { name: "Thinking" })).toBeNull();
  });
});
