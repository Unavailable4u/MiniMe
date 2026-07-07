/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,jsx}",
    "./components/**/*.{js,jsx}",
  ],
  theme: {
    extend: {
      colors: {
        cyber: {
          bg: "#05070d",       // near-black page background
          panel: "#0a0f1a",    // card / panel background
          border: "#1a2740",   // default panel/input border
          cyan: "#22d3ee",     // primary accent — buttons, active states, links
          magenta: "#f472b6",  // secondary accent — warnings, highlights
          text: "#d6e4f0",     // primary body text
          dim: "#64748b",      // secondary / muted text
        },
      },
      fontFamily: {
        display: ["Orbitron", "sans-serif"],       // headers, labels, buttons
        body: ["Rajdhani", "sans-serif"],           // paragraph / prose text
        mono: ["Share Tech Mono", "monospace"],     // code blocks
      },
      boxShadow: {
        "glow-cyan": "0 0 12px rgba(34, 211, 238, 0.35)",
        "glow-cyan-lg": "0 0 24px rgba(34, 211, 238, 0.5)",
        "glow-magenta": "0 0 12px rgba(244, 114, 182, 0.35)",
      },
      backgroundImage: {
        "cyber-grid":
          "linear-gradient(rgba(34,211,238,0.06) 1px, transparent 1px), linear-gradient(90deg, rgba(34,211,238,0.06) 1px, transparent 1px)",
      },
      backgroundSize: {
        "cyber-grid": "32px 32px",
      },
    },
  },
  plugins: [],
};