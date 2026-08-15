import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChatComposer from "@/components/ChatComposer";

afterEach(cleanup);

describe("ChatComposer", () => {
  it("Enter sends the question and clears the textarea", () => {
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} disabled={false} />);
    const textarea = screen.getByPlaceholderText("Ask a question...") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "What happens next?" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).toHaveBeenCalledWith("What happens next?");
    expect(textarea.value).toBe("");
  });

  it("Shift+Enter inserts a newline instead of sending", () => {
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} disabled={false} />);
    const textarea = screen.getByPlaceholderText("Ask a question...") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "line one" } });
    fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });

    expect(onSend).not.toHaveBeenCalled();
  });

  it("a question over 2,000 characters is truncated client-side and never sent whole", () => {
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} disabled={false} />);
    const textarea = screen.getByPlaceholderText("Ask a question...") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "x".repeat(2001) } });
    expect(textarea.value).toHaveLength(2000);

    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("x".repeat(2000));
  });

  it("does not send an empty or whitespace-only question", () => {
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} disabled={false} />);
    const textarea = screen.getByPlaceholderText("Ask a question...") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "   " } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
  });

  it("the send button is disabled while `disabled` is true", () => {
    const onSend = vi.fn();
    render(<ChatComposer onSend={onSend} disabled={true} placeholder="Chat not available" />);
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByPlaceholderText("Chat not available")).toBeDisabled();
  });
});
