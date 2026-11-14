// Tailwind theme: DESIGN.md "Crisis Tactical Utilitarian" tokens, as CSS variables (css/app.css): dark by default,
// light with data-theme="light". Built ahead of time by frontend/build.py
// into css/tailwind.css (no CDN, no in-browser compiler); run.sh and deploy.sh rebuild it automatically.
module.exports = {
  content: ["./*.html", "./js/**/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        canvas: "rgb(var(--c-canvas) / <alpha-value>)",      // layer 0
        card: "rgb(var(--c-card) / <alpha-value>)",        // layer 1
        elevated: "rgb(var(--c-elevated) / <alpha-value>)",    // layer 2
        line: "rgb(var(--c-line) / <alpha-value>)",        // 1px borders
        line2: "rgb(var(--c-line2) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",         // primary text
        muted: "rgb(var(--c-muted) / <alpha-value>)",       // secondary text
        faint: "rgb(var(--c-faint) / <alpha-value>)",
        critical: "rgb(var(--c-critical) / <alpha-value>)",    // life safety
        authority: "rgb(var(--c-authority) / <alpha-value>)",   // official / wire
        ok: "rgb(var(--c-ok) / <alpha-value>)",          // verified
        triage: "rgb(var(--c-triage) / <alpha-value>)",      // investigating / unverified
        debunk: "rgb(var(--c-debunk) / <alpha-value>)",      // false
        severe: "rgb(var(--c-severe) / <alpha-value>)",
      },
      fontFamily: {
        sans: ["Public Sans", "system-ui", "sans-serif"],   // the US government's public-service typeface
        mono: ["Public Sans", "system-ui", "sans-serif"],   // one typeface; numbers use tabular figures
      },
      borderRadius: { DEFAULT: "0.25rem", lg: "0.5rem", xl: "0.75rem" },
    },
  },
};
