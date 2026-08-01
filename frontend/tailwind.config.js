// Tailwind (Play CDN) theme for the leaderboard: palette, fonts, shadows, and animations.
// Loaded after the CDN script in index.html; kept out of the page so no config lives inline.
tailwind.config = {
  theme: {
    extend: {
      colors: {
        bg: "#FAF8F2", surface: "#FFFFFF", "surface-alt": "#F4F0E6",
        ink: { DEFAULT: "#1A2421", 2: "#3A4744" }, muted: "#717D78",
        bd: { DEFAULT: "#E5E0D5", strong: "#C8C0AB" },
        green: { deep: "#1F4D2E", DEFAULT: "#2D5F3F", soft: "#4F8A65", pale: "#E8F0E9", tint: "#F2F7F3" },
        gold: "#B8860B", silver: "#8A8A8A", bronze: "#A65A2A",
        danger: { DEFAULT: "#B5523F", pale: "#FAEAE5" },
        warn: { DEFAULT: "#B8860B", pale: "#F8EFD3" },
        add: { DEFAULT: "#1F6B3E", ink: "#144A29", soft: "rgba(31,107,62,0.12)" },
        mod: { DEFAULT: "#C77E3D", ink: "#7A4912", soft: "rgba(199,126,61,0.18)" },
        del: { DEFAULT: "#B5392C", ink: "#7A2218", soft: "rgba(181,57,44,0.14)" },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', 'sans-serif'],
        serif: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      boxShadow: {
        s1: "0 1px 0 rgba(26,36,33,0.04), 0 1px 2px rgba(26,36,33,0.04)",
        s2: "0 1px 0 rgba(26,36,33,0.05), 0 4px 12px rgba(26,36,33,0.06)",
        pop: "0 8px 24px rgba(26,36,33,0.10), 0 2px 4px rgba(26,36,33,0.06)",
      },
      keyframes: {
        rot: { to: { transform: "rotate(360deg)" } },
        pulse2: { "0%,100%": { boxShadow: "0 0 0 0 rgba(45,95,63,0)" }, "0%": { boxShadow: "0 0 0 0 rgba(45,95,63,0.5)" }, "70%": { boxShadow: "0 0 0 8px rgba(45,95,63,0)" } },
        blink2: { "50%": { opacity: "0.35" } },
        shim: { "0%": { backgroundPosition: "-200% 0" }, "100%": { backgroundPosition: "200% 0" } },
        fadein: { from: { opacity: "0", transform: "translateY(6px)" }, to: { opacity: "1", transform: "translateY(0)" } },
      },
      animation: {
        rot: "rot 1.4s linear infinite", pulse2: "pulse2 2s infinite",
        blink2: "blink2 1.4s infinite", shim: "shim 1.5s linear infinite",
        fadein: "fadein .35s ease-out both",
      },
    }
  },
};
