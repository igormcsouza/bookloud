jest.mock("@react-native-async-storage/async-storage", () =>
  require("@react-native-async-storage/async-storage/jest/async-storage-mock"),
);

import AsyncStorage from "@react-native-async-storage/async-storage";
import { clearProgress, getProgress, saveProgress } from "@/lib/progress";

describe("progress", () => {
  beforeEach(async () => {
    await AsyncStorage.clear();
  });

  it("returns null when nothing has been saved", async () => {
    expect(await getProgress("book-1")).toBeNull();
  });

  it("round-trips chunk index and position", async () => {
    await saveProgress("book-1", 3, 12_500);
    const progress = await getProgress("book-1");
    expect(progress?.chunkIndex).toBe(3);
    expect(progress?.positionMs).toBe(12_500);
    expect(progress?.updatedAt).toBeGreaterThan(0);
  });

  it("keeps different books independent", async () => {
    await saveProgress("book-1", 1, 1000);
    await saveProgress("book-2", 5, 5000);
    expect((await getProgress("book-1"))?.chunkIndex).toBe(1);
    expect((await getProgress("book-2"))?.chunkIndex).toBe(5);
  });

  it("clamps a negative position to zero", async () => {
    await saveProgress("book-1", 2, -50);
    expect((await getProgress("book-1"))?.positionMs).toBe(0);
  });

  it("ignores a chunk index below zero", async () => {
    await saveProgress("book-1", -1, 1000);
    expect(await getProgress("book-1")).toBeNull();
  });

  it("treats corrupt stored JSON as absent", async () => {
    await AsyncStorage.setItem("bookloud.progress.book-1", "{not json");
    expect(await getProgress("book-1")).toBeNull();
  });

  it("clears a saved position", async () => {
    await saveProgress("book-1", 2, 2000);
    await clearProgress("book-1");
    expect(await getProgress("book-1")).toBeNull();
  });
});
