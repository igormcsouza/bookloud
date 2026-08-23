import { Text, View } from "react-native";

type Tone = "ready" | "processing" | "attn";

const TONE_CLASSES: Record<Tone, string> = {
  ready: "bg-teal-wash dark:bg-dteal-wash",
  processing: "bg-steel-wash dark:bg-dsteel-wash",
  attn: "bg-brick-wash dark:bg-dbrick-wash",
};
const TEXT_CLASSES: Record<Tone, string> = {
  ready: "text-teal dark:text-dteal",
  processing: "text-steel dark:text-dsteel",
  attn: "text-brick dark:text-dbrick",
};

export function StatusChip({ tone, label }: { tone: Tone; label: string }) {
  return (
    <View className={`self-start rounded-full px-2.5 py-1 ${TONE_CLASSES[tone]}`}>
      <Text
        className={`text-[10px] uppercase tracking-wide ${TEXT_CLASSES[tone]}`}
        style={{ fontFamily: "IBMPlexMono_500Medium" }}
      >
        {label}
      </Text>
    </View>
  );
}
