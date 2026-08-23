const expoConfig = require("eslint-config-expo/flat");

module.exports = [
  ...expoConfig,
  {
    ignores: ["dist/*"],
  },
  {
    rules: {
      // Conflicts with usePlayback's/useBookStatus's ref-driven rAF/polling
      // loops (PLANS/phase-6.md §6) -- React-Compiler-readiness checks, not
      // applicable to this app's playback engine.
      "react-hooks/set-state-in-effect": "off",
      "react-hooks/refs": "off",
    },
  },
];
