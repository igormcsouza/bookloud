import { useState } from "react";
import { useRouter } from "expo-router";
import { FlatList, Pressable, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import * as DocumentPicker from "expo-document-picker";
import { Plus } from "lucide-react-native";
import { useBookList } from "@/hooks/useBookList";
import { BookCard } from "@/components/BookCard";
import { SwipeToDelete } from "@/components/SwipeToDelete";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import {
  createBook,
  deleteBook,
  reissueUpload,
  resynthesize,
  uploadToS3,
  type Book,
} from "@/lib/books";
import { buildUploadForm, titleFromFilename, type PickedFile } from "@/lib/upload";
import { logout } from "@/lib/auth";
import { useTheme } from "@/lib/theme";

async function pickPdf(): Promise<PickedFile | null> {
  const result = await DocumentPicker.getDocumentAsync({
    type: "application/pdf",
    copyToCacheDirectory: true,
  });
  if (result.canceled || result.assets.length === 0) return null;
  const asset = result.assets[0];
  return { uri: asset.uri, name: asset.name, mimeType: asset.mimeType, size: asset.size };
}

export default function Library() {
  const router = useRouter();
  const theme = useTheme();
  const { books, loading, error, refresh, insert, remove } = useBookList();
  const [uploading, setUploading] = useState(false);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Book | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  async function handleUpload() {
    setActionError(null);
    const file = await pickPdf().catch(() => null);
    if (!file) return;

    setUploading(true);
    try {
      const { book, upload } = await createBook(titleFromFilename(file.name));
      const form = buildUploadForm(upload, file);
      try {
        await uploadToS3(upload.url, form);
      } catch {
        // One retry with a fresh presign. Legal because the book is still
        // UPLOADED (in `_REISSUABLE_STATUSES` server-side) -- which is
        // exactly why a failed POST here orphans nothing.
        const fresh = await reissueUpload(book.id);
        await uploadToS3(fresh.url, buildUploadForm(fresh, file));
      }
      insert(book);
      router.push(`/library/add?bookId=${book.id}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function handleReupload(book: Book) {
    setActionError(null);
    const file = await pickPdf().catch(() => null);
    if (!file) return;
    setUploading(true);
    try {
      const upload = await reissueUpload(book.id);
      const form = buildUploadForm(upload, file);
      await uploadToS3(upload.url, form);
      await refresh();
      router.push(`/library/add?bookId=${book.id}`);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Re-upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function handleRetry(book: Book) {
    setRetryingId(book.id);
    setActionError(null);
    try {
      await resynthesize(book.id);
      await refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not retry audio for this book.");
    } finally {
      setRetryingId(null);
    }
  }

  async function handleDelete(book: Book) {
    setDeletingId(book.id);
    setActionError(null);
    try {
      await deleteBook(book.id);
      remove(book.id);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Could not delete this book.");
    } finally {
      setDeletingId(null);
    }
  }

  const needsAttention = books.filter((b) => b.status === "PARTIAL" || b.status === "FAILED").length;

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg px-5 pt-4">
      <View className="flex-row items-baseline justify-between mb-1">
        <View>
          <Text className="text-[27px] text-ink dark:text-dink" style={{ fontFamily: "Fraunces_600SemiBold" }}>
            Your books
          </Text>
          <Text className="text-[14px] text-ink-muted dark:text-dink-muted mb-3">
            {loading
              ? "Loading books…"
              : `${books.length} book${books.length === 1 ? "" : "s"}${
                  needsAttention > 0 ? ` · ${needsAttention} need${needsAttention === 1 ? "s" : ""} a look` : ""
                }`}
          </Text>
        </View>
        <Pressable onPress={() => logout().then(() => router.replace("/sign-in"))} hitSlop={8}>
          <Text className="text-[12px] text-ink-muted dark:text-dink-muted underline">Sign out</Text>
        </Pressable>
      </View>

      {(error || actionError) && (
        <View className="bg-brick-wash dark:bg-dbrick-wash rounded-md px-4 py-3 mb-3">
          <Text className="text-brick dark:text-dbrick text-[13px]">{error || actionError}</Text>
        </View>
      )}

      {!loading && books.length === 0 ? (
        <View className="flex-1 items-center justify-center px-8">
          <Text className="text-ink-muted dark:text-dink-muted text-center text-[14px]">
            No books yet. Upload a PDF to have it read aloud, word by word.
          </Text>
        </View>
      ) : (
        <FlatList
          data={books}
          keyExtractor={(item) => item.id}
          contentContainerStyle={{ gap: 10, paddingBottom: 100 }}
          onRefresh={refresh}
          refreshing={loading}
          renderItem={({ item }) => (
            <SwipeToDelete
              disabled={deletingId === item.id}
              onDelete={() => setPendingDelete(item)}
            >
              <BookCard
                book={item}
                retrying={retryingId === item.id}
                reuploading={uploading}
                onPress={() => {
                  if (item.status === "FAILED") return;
                  if (item.status === "READY" || item.status === "PARTIAL") {
                    router.push(`/reader/${item.id}`);
                  } else {
                    router.push(`/library/add?bookId=${item.id}`);
                  }
                }}
                onRetry={() => handleRetry(item)}
                onReupload={() => handleReupload(item)}
              />
            </SwipeToDelete>
          )}
        />
      )}

      <Pressable
        onPress={handleUpload}
        disabled={uploading}
        className="absolute right-5 bottom-7 w-14 h-14 rounded-full bg-accent dark:bg-daccent items-center justify-center"
        style={{ opacity: uploading ? 0.6 : 1, elevation: 4 }}
      >
        <Plus size={26} color={theme.accentInk} strokeWidth={2.5} />
      </Pressable>

      <ConfirmDialog
        visible={pendingDelete !== null}
        title="Delete this book?"
        message={
          pendingDelete
            ? `"${pendingDelete.title}" and its audio will be permanently deleted. This can't be undone.`
            : ""
        }
        confirmLabel="Delete"
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => {
          const book = pendingDelete;
          setPendingDelete(null);
          if (book) void handleDelete(book);
        }}
      />
    </SafeAreaView>
  );
}
