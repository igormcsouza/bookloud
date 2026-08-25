import { Pressable, Text, View } from "react-native";
import { ArrowRight } from "lucide-react-native";
import type { Book } from "@/lib/books";
import { StatusChip } from "@/components/StatusChip";
import { useTheme } from "@/lib/theme";

const COVER_PALETTE = [
  { bg: "bg-teal-wash dark:bg-dteal-wash", text: "text-teal dark:text-dteal" },
  { bg: "bg-steel-wash dark:bg-dsteel-wash", text: "text-steel dark:text-dsteel" },
  { bg: "bg-brick-wash dark:bg-dbrick-wash", text: "text-brick dark:text-dbrick" },
];

function paletteFor(title: string) {
  const code = title.charCodeAt(0) || 0;
  return COVER_PALETTE[code % COVER_PALETTE.length];
}

function formatDuration(ms: number): string {
  const totalMinutes = Math.round(ms / 60000);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

type Props = {
  book: Book;
  onPress: () => void;
  onRetry: () => void;
  onReupload: () => void;
  retrying?: boolean;
  /** Disables the re-upload link while a pick/upload is already in flight
   *  (for this book or any other) -- without it, a fast double-tap opens
   *  the document picker twice and fires two concurrent upload flows. */
  reuploading?: boolean;
  /** DELETE round-trip in flight (issue #12 follow-up): the card goes
   *  greyscale/unpressable with a "Deleting…" chip so the multi-second gap
   *  before the row disappears from the list reads as "working", not as an
   *  unresponsive swipe. */
  deleting?: boolean;
};

export function BookCard({ book, onPress, onRetry, onReupload, retrying, reuploading, deleting }: Props) {
  const theme = useTheme();
  const palette = paletteFor(book.title);
  const initial = book.title.charAt(0).toUpperCase();
  const percent =
    book.chunksTotal > 0 ? Math.round((book.chunksDone / book.chunksTotal) * 100) : 0;

  return (
    <Pressable
      onPress={onPress}
      disabled={deleting}
      className="bg-surface dark:bg-dsurface border border-border dark:border-dborder rounded-md p-3.5 flex-row gap-3"
      style={deleting ? { opacity: 0.5 } : undefined}
    >
      <View
        className={`w-12 h-16 rounded-md items-center justify-center ${deleting ? "bg-border dark:bg-dborder" : palette.bg}`}
      >
        <Text
          className={`text-xl ${deleting ? "text-ink-muted dark:text-dink-muted" : palette.text}`}
          style={{ fontFamily: "Fraunces_600SemiBold" }}
        >
          {initial}
        </Text>
      </View>

      <View className="flex-1 min-w-0">
        <Text
          numberOfLines={2}
          className="text-[14.5px] text-ink dark:text-dink"
          style={{ fontFamily: "Karla_700Bold" }}
        >
          {book.title}
        </Text>

        <View className="mt-1.5">
          {deleting && <StatusChip tone="processing" label="Deleting…" />}
          {!deleting && book.status === "READY" && (
            <StatusChip tone="ready" label={`Ready · ${formatDuration(book.audioDurationMs)}`} />
          )}
          {!deleting &&
            (book.status === "EXTRACTING" || book.status === "STITCHING" || book.status === "UPLOADED" || book.status === "EXTRACTED") && (
              <StatusChip
                tone="processing"
                label={
                  book.status === "STITCHING"
                    ? "Stitching the audiobook"
                    : `Recording voice · ${book.chunksDone}/${book.chunksTotal}`
                }
              />
            )}
          {!deleting && book.status === "PARTIAL" && <StatusChip tone="attn" label="Needs attention" />}
          {!deleting && book.status === "FAILED" && <StatusChip tone="attn" label="Couldn't read this PDF" />}
        </View>

        {!deleting && (book.status === "EXTRACTING" || book.status === "STITCHING") && (
          <View className="h-1.5 rounded-full bg-border dark:bg-dborder mt-1.5 overflow-hidden">
            <View className="h-full rounded-full bg-accent dark:bg-daccent" style={{ width: `${percent}%` }} />
          </View>
        )}

        {!deleting && book.status === "PARTIAL" && (
          <>
            <Text className="text-[11.5px] text-ink-muted dark:text-dink-muted mt-1">
              {book.chunksFailed} section{book.chunksFailed === 1 ? "" : "s"} couldn&apos;t be read aloud
              — playback still works, just skips them.
            </Text>
            <Pressable onPress={onRetry} disabled={retrying} hitSlop={8} className="flex-row items-center gap-1 mt-1.5">
              <Text className="text-[12px] text-brick dark:text-dbrick underline" style={{ fontFamily: "Karla_700Bold" }}>
                {retrying ? "Queued — check back shortly" : "Retry those sections"}
              </Text>
              {!retrying && <ArrowRight size={12} color={theme.brick} strokeWidth={2.5} />}
            </Pressable>
          </>
        )}

        {!deleting && book.status === "FAILED" && (
          <Pressable onPress={onReupload} disabled={reuploading} hitSlop={8} className="flex-row items-center gap-1 mt-1.5">
            <Text className="text-[12px] text-brick dark:text-dbrick underline" style={{ fontFamily: "Karla_700Bold" }}>
              {reuploading ? "Uploading…" : "Re-upload"}
            </Text>
            {!reuploading && <ArrowRight size={12} color={theme.brick} strokeWidth={2.5} />}
          </Pressable>
        )}
      </View>
    </Pressable>
  );
}
