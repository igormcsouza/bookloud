/** @type {import('tailwindcss').Config} */
// Colors ported 1:1 from the issue #10 design mock's CSS custom properties
// (light values as the base tokens, dark values under the `dark:` variant).
// `darkMode: "media"` lets NativeWind follow the OS color scheme
// automatically -- the mock is explicitly light+dark, unlike cashlytics'
// dark-only theme.
module.exports = {
  content: ["./app/**/*.{js,jsx,ts,tsx}", "./components/**/*.{js,jsx,ts,tsx}"],
  presets: [require("nativewind/preset")],
  darkMode: "media",
  theme: {
    extend: {
      colors: {
        bg: "#F2EEE4",
        surface: "#FBF8F1",
        "surface-raised": "#FFFFFF",
        ink: "#221F1A",
        "ink-muted": "#7A7266",
        "ink-faint": "#A79E8E",
        border: "#E1D8C4",
        bezel: "#E9E2D2",
        accent: "#B87424",
        "accent-ink": "#FFF8EC",
        "accent-wash": "#F3E3CB",
        teal: "#2F7C6A",
        "teal-wash": "#DCEEE8",
        steel: "#5C6E90",
        "steel-wash": "#E4E8F0",
        brick: "#A2452F",
        "brick-wash": "#F1DCD3",

        "dbg": "#14171F",
        "dsurface": "#1C202A",
        "dsurface-raised": "#232838",
        "dink": "#ECE7DA",
        "dink-muted": "#9C948A",
        "dink-faint": "#6C6459",
        "dborder": "#2C3140",
        "dbezel": "#2A2E3A",
        "daccent": "#E3A83B",
        "daccent-ink": "#201404",
        "daccent-wash": "#34290F",
        "dteal": "#6FC2AC",
        "dteal-wash": "#1B2E2A",
        "dsteel": "#94A9D6",
        "dsteel-wash": "#232C42",
        "dbrick": "#E28468",
        "dbrick-wash": "#34211B",
      },
      borderRadius: {
        sm: "10px",
        md: "16px",
        lg: "28px",
      },
    },
  },
  plugins: [],
};
