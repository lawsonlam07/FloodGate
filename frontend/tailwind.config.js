// Tailwind theme: DESIGN.md "Crisis Tactical Utilitarian" tokens. Built ahead of time by frontend/build.py
// into css/tailwind.css (no CDN, no in-browser compiler); run.sh and deploy.sh rebuild it automatically.
module.exports = {
  content: ["./*.html", "./js/**/*.js"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        canvas: "#0B111A",      // layer 0
        card: "#131D2A",        // layer 1
        elevated: "#1C2838",    // layer 2
        line: "#26354A",        // 1px borders
        line2: "#33465E",
        ink: "#F8FAFC",         // primary text
        muted: "#94A3B8",       // secondary text
        faint: "#64748B",
        critical: "#EF4444",    // life safety
        authority: "#38BDF8",   // official / wire
        ok: "#10B981",          // verified
        triage: "#F59E0B",      // investigating / unverified
        debunk: "#991B1B",      // false
        severe: "#F97316",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: { DEFAULT: "0.25rem", lg: "0.5rem", xl: "0.75rem" },
    },
  },
};
