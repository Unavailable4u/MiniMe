/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,jsx}",
    "./components/**/*.{js,jsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Retargeted into the neutral dark scale (see globals.css) so
        // text-cyber-*/bg-cyber-*/border-cyber-* utility classes render
        // identically to the var(--cyber-*) usages elsewhere — this app
        // no longer runs a separate cyan/magenta cyberpunk palette.
        cyber: {
          bg: "var(--neutral-950)",     // page background
          panel: "var(--neutral-900)",  // card / panel background
          border: "var(--neutral-800)", // default panel/input border
          cyan: "var(--cyber-cyan)",    // primary accent — buttons, active states, links (brand crimson)
          magenta: "var(--cyber-magenta)", // danger accent — warnings, destructive actions (brand crimson)
          text: "var(--neutral-100)",   // primary body text
          dim: "var(--neutral-500)",    // secondary / muted text
        },
      },
      fontFamily: {
        display: ["Sora", "sans-serif"],            // headers, labels, buttons
        body: ["Inter", "sans-serif"],               // paragraph / prose text
        mono: ["JetBrains Mono", "monospace"],       // code blocks
      },
      boxShadow: {
        // Was a colored neon blur; now a plain subtle ring (matches
        // --cyber-glow in globals.css).
        "glow-cyan": "0 0 0 1px rgba(255, 255, 255, 0.08)",
        "glow-cyan-lg": "0 0 0 1px rgba(255, 255, 255, 0.12)",
        "glow-magenta": "0 0 0 1px rgba(255, 32, 82, 0.25)",
      },
      backgroundImage: {
        "cyber-grid":
          "linear-gradient(rgba(255,255,255,0.04) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.04) 1px, transparent 1px)",
      },
      backgroundSize: {
        "cyber-grid": "32px 32px",
      },
    },
  },
  plugins: [],
};