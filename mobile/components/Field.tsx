import { TextInput, type TextInputProps } from "react-native";
import { useTheme } from "@/lib/theme";

export function Field(props: TextInputProps) {
  const theme = useTheme();
  return (
    <TextInput
      placeholderTextColor={theme.textFaint}
      className="bg-surface dark:bg-dsurface border border-border dark:border-dborder rounded-sm px-4 py-3.5 text-[15px] text-ink dark:text-dink mb-2.5"
      style={{ fontFamily: "Karla_400Regular" }}
      {...props}
    />
  );
}
