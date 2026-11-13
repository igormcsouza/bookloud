import { useState } from "react";
import { Image, Pressable, Text, View } from "react-native";
import { LogOut } from "lucide-react-native";
import { useTheme } from "@/lib/theme";

type Props = { name: string; picture: string | null; onPress: () => void };

/** Header chip: the signed-in user's Citadel photo, first name and a sign-out glyph. The whole chip is the
 *  button (the caller confirms before signing out). Initial replaces the photo when it is missing or fails. */
export function UserChip({ name, picture, onPress }: Props) {
  const theme = useTheme();
  const [failed, setFailed] = useState(false);
  const first = name.split(/\s+/)[0] || name;

  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Signed in as ${first}. Sign out`}
      hitSlop={6}
      className="h-9 flex-row items-center rounded-full border border-border dark:border-dborder bg-surface-raised dark:bg-dsurface-raised pl-1 pr-3"
      style={{ maxWidth: "55%", flexShrink: 1 }}
    >
      {picture && !failed ? (
        <Image
          source={{ uri: picture }}
          onError={() => setFailed(true)}
          className="w-7 h-7 rounded-full bg-accent-wash dark:bg-daccent-wash"
          accessibilityIgnoresInvertColors
        />
      ) : (
        <View className="w-7 h-7 rounded-full bg-accent-wash dark:bg-daccent-wash items-center justify-center">
          <Text className="text-[13px] text-accent dark:text-daccent" style={{ fontFamily: "Karla_700Bold" }}>
            {first.charAt(0).toUpperCase()}
          </Text>
        </View>
      )}
      <Text
        numberOfLines={1}
        className="ml-2 mr-2 flex-shrink text-[13px] text-ink dark:text-dink"
        style={{ fontFamily: "Karla_600SemiBold" }}
      >
        {first}
      </Text>
      <LogOut size={15} color={theme.textMuted} strokeWidth={2} />
    </Pressable>
  );
}
