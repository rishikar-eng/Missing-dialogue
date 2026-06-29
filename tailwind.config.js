/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // All colors resolve to RGB triplet CSS variables so they swap
        // when `<html data-theme="light|dark">` flips. Tailwind's alpha
        // syntax (`bg-ink-800/40`) still works.
        ink: {
          50:  "rgb(var(--ink-50)  / <alpha-value>)",
          100: "rgb(var(--ink-100) / <alpha-value>)",
          200: "rgb(var(--ink-200) / <alpha-value>)",
          300: "rgb(var(--ink-300) / <alpha-value>)",
          400: "rgb(var(--ink-400) / <alpha-value>)",
          500: "rgb(var(--ink-500) / <alpha-value>)",
          600: "rgb(var(--ink-600) / <alpha-value>)",
          700: "rgb(var(--ink-700) / <alpha-value>)",
          800: "rgb(var(--ink-800) / <alpha-value>)",
          900: "rgb(var(--ink-900) / <alpha-value>)",
          950: "rgb(var(--ink-950) / <alpha-value>)",
        },
        amber: {
          DEFAULT: "rgb(var(--amber) / <alpha-value>)",
          soft:    "rgb(var(--amber-soft) / <alpha-value>)",
          deep:    "rgb(var(--amber-deep) / <alpha-value>)",
        },
        coral:   { DEFAULT: "rgb(var(--coral) / <alpha-value>)" },
        active:  { DEFAULT: "rgb(var(--amber) / <alpha-value>)" },
        accent2: { DEFAULT: "rgb(var(--coral) / <alpha-value>)" },
        ok:      { DEFAULT: "rgb(var(--ok) / <alpha-value>)" },
        warn:    { DEFAULT: "rgb(var(--amber) / <alpha-value>)" },
        err:     { DEFAULT: "rgb(var(--err) / <alpha-value>)" },
      },
      fontFamily: {
        sans:    ["'Instrument Sans'", "ui-sans-serif", "system-ui", "sans-serif"],
        display: ["Fraunces", "Georgia", "serif"],
        mono:    ["'JetBrains Mono'", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};
