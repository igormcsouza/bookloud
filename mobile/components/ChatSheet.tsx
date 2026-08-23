import { forwardRef, useMemo, useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";
import BottomSheet, { BottomSheetFlatList } from "@gorhom/bottom-sheet";
import { useChat } from "@/hooks/useChat";
import type { ChatMessage } from "@/lib/books";

type Props = {
  bookId: string;
  chapterLabel: string;
  anchorRef: React.MutableRefObject<number>;
  positionMs: number;
  onClose: () => void;
};

type Row =
  | { kind: "message"; message: ChatMessage }
  | { kind: "streaming"; text: string };

export const ChatSheet = forwardRef<BottomSheet, Props>(function ChatSheet(
  { bookId, chapterLabel, anchorRef, positionMs, onClose },
  ref,
) {
  const { messages, chat, isStreaming, streamingText, streamingError, sendQuestion } = useChat(
    bookId,
    anchorRef,
  );
  const [question, setQuestion] = useState("");

  const rows: Row[] = useMemo(() => {
    const base: Row[] = messages.map((m) => ({ kind: "message", message: m }));
    if (isStreaming) base.push({ kind: "streaming", text: streamingText });
    return base;
  }, [messages, isStreaming, streamingText]);

  const quotaReached = chat && chat.usedToday >= chat.dailyLimit;
  const disabled = !question.trim() || isStreaming || Boolean(quotaReached);

  function handleSend() {
    if (disabled) return;
    const text = question;
    setQuestion("");
    void sendQuestion(text, positionMs);
  }

  return (
    <BottomSheet ref={ref} index={-1} snapPoints={["78%"]} enablePanDownToClose onClose={onClose}>
      <View className="flex-row items-center justify-between px-4 pb-3 border-b border-border dark:border-dborder">
        <Text className="text-[13.5px] text-ink dark:text-dink" style={{ fontFamily: "Karla_700Bold" }}>
          Ask about this page
        </Text>
        <View className="flex-row items-center gap-2">
          <View className="bg-bg dark:bg-dbg border border-border dark:border-dborder rounded-full px-2.5 py-1">
            <Text className="text-[10px] text-ink-muted dark:text-dink-muted" style={{ fontFamily: "IBMPlexMono_400Regular" }}>
              {chapterLabel}
            </Text>
          </View>
          <Pressable onPress={onClose} hitSlop={8}>
            <Text className="text-ink-muted dark:text-dink-muted text-lg">✕</Text>
          </Pressable>
        </View>
      </View>

      {quotaReached && (
        <View className="mx-4 mt-3 bg-surface dark:bg-dsurface border border-accent dark:border-daccent rounded-md px-3 py-2.5">
          <Text className="text-[11px] text-ink dark:text-dink">
            You&apos;ve reached today&apos;s question limit ({chat?.dailyLimit}). It resets at midnight UTC.
          </Text>
        </View>
      )}

      {streamingError && (
        <View className="mx-4 mt-3 bg-brick-wash dark:bg-dbrick-wash rounded-md px-3 py-2.5">
          <Text className="text-[11px] text-brick dark:text-dbrick">{streamingError.message}</Text>
        </View>
      )}

      <BottomSheetFlatList
        data={rows}
        keyExtractor={(_, i) => String(i)}
        contentContainerStyle={{ padding: 16, gap: 10 }}
        renderItem={({ item }) => {
          if (item.kind === "streaming") {
            return (
              <View className="self-start bg-teal-wash dark:bg-dteal-wash rounded-2xl px-3.5 py-2.5 max-w-[86%]">
                <Text className="text-[13px] text-ink dark:text-dink leading-5">{item.text || "…"}</Text>
              </View>
            );
          }
          const isUser = item.message.role === "user";
          return (
            <View
              className={`rounded-2xl px-3.5 py-2.5 max-w-[86%] ${
                isUser
                  ? "self-end bg-accent-wash dark:bg-daccent-wash"
                  : "self-start bg-teal-wash dark:bg-dteal-wash"
              }`}
            >
              <Text className="text-[13px] text-ink dark:text-dink leading-5">{item.message.content}</Text>
            </View>
          );
        }}
      />

      <View className="flex-row items-end gap-2 px-4 pb-5 pt-3 border-t border-border dark:border-dborder">
        <TextInput
          value={question}
          onChangeText={setQuestion}
          placeholder="Ask a question…"
          placeholderTextColor="#A79E8E"
          multiline
          maxLength={2000}
          className="flex-1 bg-surface dark:bg-dsurface border border-border dark:border-dborder rounded-sm px-3 py-2.5 text-[13px] text-ink dark:text-dink max-h-[80px]"
        />
        <Pressable
          onPress={handleSend}
          disabled={disabled}
          className="w-[38px] h-[38px] rounded-full bg-accent dark:bg-daccent items-center justify-center"
          style={{ opacity: disabled ? 0.4 : 1 }}
        >
          <Text className="text-accent-ink dark:text-daccent-ink">↑</Text>
        </Pressable>
      </View>
    </BottomSheet>
  );
});
