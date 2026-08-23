import { useLocalSearchParams, useRouter } from "expo-router";
import { Pressable, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { ArrowRight, Check } from "lucide-react-native";
import { useBookStatus } from "@/hooks/useBookStatus";
import type { BookStatus } from "@/lib/books";
import { PrimaryButton } from "@/components/PrimaryButton";
import { useTheme, type Theme } from "@/lib/theme";

type StepState = "done" | "active" | "pending";

const STEPS: { key: string; label: string }[] = [
  { key: "uploaded", label: "Uploaded" },
  { key: "read", label: "Read the pages" },
  { key: "voice", label: "Recording the voice" },
  { key: "stitch", label: "Stitching the audiobook" },
  { key: "ready", label: "Ready to open" },
];

function stepStates(status: BookStatus | undefined, terminal: boolean): StepState[] {
  if (!status) return ["active", "pending", "pending", "pending", "pending"];
  if (status === "FAILED") return ["done", "active", "pending", "pending", "pending"];

  const order: BookStatus[] = ["UPLOADED", "EXTRACTING", "EXTRACTED", "STITCHING"];
  const index = order.indexOf(status);
  // index -1 means READY/PARTIAL directly (status already terminal)
  const done = (n: number) => index > n || terminal;
  const active = (n: number) => index === n;

  return [
    "done",
    done(1) ? "done" : active(1) ? "active" : "pending",
    done(2) ? "done" : active(2) ? "active" : "pending",
    done(3) ? "done" : active(3) ? "active" : "pending",
    terminal ? "done" : "pending",
  ];
}

function Dot({ state, theme }: { state: StepState; theme: Theme }) {
  if (state === "done") {
    return (
      <View className="w-[26px] h-[26px] rounded-full bg-teal dark:bg-dteal items-center justify-center">
        <Check size={14} color={theme.surface} strokeWidth={3} />
      </View>
    );
  }
  if (state === "active") {
    return (
      <View className="w-[26px] h-[26px] rounded-full border-2 border-accent dark:border-daccent bg-accent-wash dark:bg-daccent-wash items-center justify-center">
        <View className="w-2 h-2 rounded-full bg-accent dark:bg-daccent" />
      </View>
    );
  }
  return <View className="w-[26px] h-[26px] rounded-full border-2 border-border dark:border-dborder" />;
}

export default function AddBook() {
  const { bookId } = useLocalSearchParams<{ bookId: string }>();
  const router = useRouter();
  const theme = useTheme();
  const { status, notFound } = useBookStatus(bookId ?? null);

  if (notFound) {
    return (
      <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg items-center justify-center px-8">
        <Text className="text-ink dark:text-dink text-center mb-4">This book no longer exists.</Text>
        <PrimaryButton label="Back to library" onPress={() => router.replace("/library")} />
      </SafeAreaView>
    );
  }

  const states = stepStates(status?.status, status?.terminal ?? false);
  const failed = status?.status === "FAILED";
  const ready = status?.terminal && !failed;

  return (
    <SafeAreaView edges={["top", "bottom"]} className="flex-1 bg-bg dark:bg-dbg px-6 pt-6">
      <Text className="text-[23px] text-ink dark:text-dink mb-1" style={{ fontFamily: "Fraunces_600SemiBold" }}>
        Adding your book
      </Text>
      {status && (
        <Text className="text-[14px] text-ink-muted dark:text-dink-muted mb-5">
          {status.progress.chunksTotal > 0
            ? `${status.progress.chunksTotal} section${status.progress.chunksTotal === 1 ? "" : "s"} found`
            : "Getting started…"}
        </Text>
      )}

      <View className="gap-4">
        {STEPS.map((step, i) => (
          <View key={step.key} className="flex-row gap-3 items-start">
            <Dot state={states[i]} theme={theme} />
            <View className="flex-1">
              <Text
                className={`text-[14.5px] ${states[i] === "pending" ? "text-ink-faint dark:text-dink-faint" : "text-ink dark:text-dink"}`}
                style={{ fontFamily: "Karla_600SemiBold" }}
              >
                {step.label}
              </Text>
              {step.key === "voice" && status && states[i] === "active" && (
                <>
                  <Text className="text-[12px] text-ink-muted dark:text-dink-muted mt-0.5">
                    {status.progress.chunksDone} of {status.progress.chunksTotal} sections
                  </Text>
                  <View className="h-1.5 w-40 rounded-full bg-border dark:bg-dborder mt-1.5 overflow-hidden">
                    <View
                      className="h-full rounded-full bg-accent dark:bg-daccent"
                      style={{ width: `${status.progress.percent}%` }}
                    />
                  </View>
                </>
              )}
            </View>
          </View>
        ))}
      </View>

      {failed && (
        <View className="mt-6">
          <Text className="text-brick dark:text-dbrick text-[13px] mb-3">
            {status?.failureReason || "This PDF couldn't be read."}
          </Text>
          <Pressable onPress={() => router.replace("/library")} className="flex-row items-center gap-1">
            <Text className="text-brick dark:text-dbrick underline" style={{ fontFamily: "Karla_700Bold" }}>
              Back to library to re-upload
            </Text>
            <ArrowRight size={14} color={theme.brick} strokeWidth={2.5} />
          </Pressable>
        </View>
      )}

      {ready && (
        <View className="mt-6">
          <PrimaryButton label="Open book" onPress={() => router.replace(`/reader/${bookId}`)} />
        </View>
      )}
    </SafeAreaView>
  );
}
