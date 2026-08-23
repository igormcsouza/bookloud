// Design tokens ported 1:1 from the issue #10 design mock's CSS custom
// properties. Used for non-className contexts (icon colors, StatusBar
// style) -- NativeWind's `dark:` classes (tailwind.config.js) cover
// everything else.

import { useColorScheme } from "react-native";

export const lightTheme = {
  bg: "#F2EEE4",
  surface: "#FBF8F1",
  surfaceRaised: "#FFFFFF",
  text: "#221F1A",
  textMuted: "#7A7266",
  textFaint: "#A79E8E",
  border: "#E1D8C4",
  accent: "#B87424",
  accentInk: "#FFF8EC",
  accentWash: "#F3E3CB",
  teal: "#2F7C6A",
  tealWash: "#DCEEE8",
  steel: "#5C6E90",
  steelWash: "#E4E8F0",
  brick: "#A2452F",
  brickWash: "#F1DCD3",
};

export const darkTheme = {
  bg: "#14171F",
  surface: "#1C202A",
  surfaceRaised: "#232838",
  text: "#ECE7DA",
  textMuted: "#9C948A",
  textFaint: "#6C6459",
  border: "#2C3140",
  accent: "#E3A83B",
  accentInk: "#201404",
  accentWash: "#34290F",
  teal: "#6FC2AC",
  tealWash: "#1B2E2A",
  steel: "#94A9D6",
  steelWash: "#232C42",
  brick: "#E28468",
  brickWash: "#34211B",
};

export type Theme = typeof lightTheme;

export function useTheme(): Theme {
  const scheme = useColorScheme();
  return scheme === "dark" ? darkTheme : lightTheme;
}
