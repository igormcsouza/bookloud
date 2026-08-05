import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // "Reading-lamp" palette: a library-at-night feel for a PDF reader —
        // warm-dark ink instead of cool slate, moss green standing in for
        // the brass banker's-lamp glow. Errors stay on the default rose/red
        // scale on purpose: green-for-status, red-for-error keeps the two
        // unambiguous rather than competing on the same hue.
        ink: {
          950: "#0d1310",
          900: "#141d18",
          800: "#212f27",
          700: "#2f4038",
        },
        sage: "#93a89c",
        paper: "#e9f1ec",
        moss: {
          300: "#7cc79a",
          400: "#4f9d6e",
          500: "#3c8759",
        },
      },
    },
  },
  plugins: [],
};

export default config;
