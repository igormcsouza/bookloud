// Local "resume where I left off" storage (issue #22). Per-book, on-device
// only -- no backend involvement, matching the issue's own ask for "the
// actual storage" to live in the local database rather than a sync'd one.

import AsyncStorage from "@react-native-async-storage/async-storage";

export type ReadingProgress = {
  chunkIndex: number;
  positionMs: number;
  updatedAt: number;
};

const KEY_PREFIX = "bookloud.progress.";

function keyFor(bookId: string): string {
  return `${KEY_PREFIX}${bookId}`;
}

export async function getProgress(bookId: string): Promise<ReadingProgress | null> {
  try {
    const raw = await AsyncStorage.getItem(keyFor(bookId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ReadingProgress>;
    if (typeof parsed.chunkIndex !== "number" || typeof parsed.positionMs !== "number") return null;
    return {
      chunkIndex: parsed.chunkIndex,
      positionMs: parsed.positionMs,
      updatedAt: typeof parsed.updatedAt === "number" ? parsed.updatedAt : 0,
    };
  } catch {
    // Corrupt/foreign JSON under our key -- treat as "nothing saved" rather
    // than surfacing a crash over what is purely a convenience feature.
    return null;
  }
}

export async function saveProgress(
  bookId: string,
  chunkIndex: number,
  positionMs: number,
): Promise<void> {
  if (chunkIndex < 0) return;
  const progress: ReadingProgress = { chunkIndex, positionMs: Math.max(0, positionMs), updatedAt: Date.now() };
  try {
    await AsyncStorage.setItem(keyFor(bookId), JSON.stringify(progress));
  } catch {
    // Best-effort; losing the resume point is not worth surfacing an error.
  }
}

export async function clearProgress(bookId: string): Promise<void> {
  try {
    await AsyncStorage.removeItem(keyFor(bookId));
  } catch {
    // Best-effort.
  }
}
