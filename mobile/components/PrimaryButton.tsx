import { Pressable, Text, ActivityIndicator } from "react-native";
import { useTheme } from "@/lib/theme";

type Props = {
  label: string;
  onPress: () => void;
  disabled?: boolean;
  loading?: boolean;
};

export function PrimaryButton({ label, onPress, disabled, loading }: Props) {
  const theme = useTheme();
  const isDisabled = disabled || loading;
  return (
    <Pressable
      onPress={onPress}
      disabled={isDisabled}
      className="bg-accent dark:bg-daccent rounded-full py-3.5 mt-1.5 items-center justify-center flex-row gap-2"
      style={{ opacity: isDisabled ? 0.6 : 1 }}
    >
      {loading && <ActivityIndicator color={theme.accentInk} size="small" />}
      <Text
        style={{ fontFamily: "Karla_700Bold", color: theme.accentInk }}
        className="text-[15px]"
      >
        {label}
      </Text>
    </Pressable>
  );
}
