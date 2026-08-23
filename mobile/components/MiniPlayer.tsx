import { Pressable, Text, View } from "react-native";
import { Pause, Play, RotateCcw, RotateCw } from "lucide-react-native";
import { PLAYBACK_RATES } from "@/hooks/usePlayback";
import { useTheme } from "@/lib/theme";

function formatTime(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

type Props = {
  positionMs: number;
  durationMs: number;
  playing: boolean;
  playbackRate: number;
  disabled: boolean;
  onToggle: () => void;
  onSeekMs: (ms: number) => void;
  onSetRate: (rate: number) => void;
};

export function MiniPlayer({
  positionMs,
  durationMs,
  playing,
  playbackRate,
  disabled,
  onToggle,
  onSeekMs,
  onSetRate,
}: Props) {
  const theme = useTheme();
  const percent = durationMs > 0 ? Math.min(100, (positionMs / durationMs) * 100) : 0;

  function cycleRate() {
    const idx = PLAYBACK_RATES.indexOf(playbackRate as (typeof PLAYBACK_RATES)[number]);
    const next = PLAYBACK_RATES[(idx + 1) % PLAYBACK_RATES.length];
    onSetRate(next);
  }

  return (
    <View className="bg-surface dark:bg-dsurface border border-border dark:border-dborder rounded-md px-4 pt-3.5 pb-4 relative">
      <Pressable
        onPress={cycleRate}
        className="absolute -top-3.5 right-4 bg-surface-raised dark:bg-dsurface-raised border border-border dark:border-dborder rounded-full px-3 py-1"
      >
        <Text className="text-[11px] text-ink dark:text-dink" style={{ fontFamily: "IBMPlexMono_500Medium" }}>
          {playbackRate}×
        </Text>
      </Pressable>

      <View className="flex-row justify-between mb-2">
        <Text className="text-[11px] text-ink-muted dark:text-dink-muted" style={{ fontFamily: "IBMPlexMono_400Regular" }}>
          {formatTime(positionMs)}
        </Text>
        <Text className="text-[11px] text-ink-muted dark:text-dink-muted" style={{ fontFamily: "IBMPlexMono_400Regular" }}>
          {formatTime(durationMs)}
        </Text>
      </View>

      <View className="h-1 rounded-full bg-border dark:bg-dborder mb-3.5 overflow-hidden">
        <View className="h-full bg-accent dark:bg-daccent" style={{ width: `${percent}%` }} />
      </View>

      <View className="flex-row items-center justify-center gap-8">
        <Pressable
          onPress={() => onSeekMs(positionMs - 15_000)}
          disabled={disabled}
          hitSlop={10}
          className="items-center"
        >
          <RotateCcw size={22} color={theme.textMuted} strokeWidth={2} />
          <Text
            className="text-ink-muted dark:text-dink-muted text-[9px] mt-0.5"
            style={{ fontFamily: "IBMPlexMono_500Medium" }}
          >
            15
          </Text>
        </Pressable>
        <Pressable
          onPress={onToggle}
          disabled={disabled}
          className="w-14 h-14 rounded-full bg-accent dark:bg-daccent items-center justify-center"
          style={{ opacity: disabled ? 0.5 : 1 }}
        >
          {playing ? (
            <Pause size={24} color={theme.accentInk} fill={theme.accentInk} strokeWidth={0} />
          ) : (
            <Play size={24} color={theme.accentInk} fill={theme.accentInk} strokeWidth={0} />
          )}
        </Pressable>
        <Pressable
          onPress={() => onSeekMs(positionMs + 15_000)}
          disabled={disabled}
          hitSlop={10}
          className="items-center"
        >
          <RotateCw size={22} color={theme.textMuted} strokeWidth={2} />
          <Text
            className="text-ink-muted dark:text-dink-muted text-[9px] mt-0.5"
            style={{ fontFamily: "IBMPlexMono_500Medium" }}
          >
            15
          </Text>
        </Pressable>
      </View>
    </View>
  );
}
