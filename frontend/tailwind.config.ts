import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        // AbhiAya brand tokens (locked-in palette — see design sign-off).
        "charcoal-char": "#1E1712", // near-black w/ warmth — login bg, nav/header chrome
        "cast-iron": "#2B2420", // primary body text
        "ash-flour": "#F7F4F0", // card/surface
        "roasted-almond": "#EBE4DB", // page bg
        "marigold-saffron": "#E8A33D", // restaurant accent + primary CTA
        "curry-leaf": "#2F5233", // admin accent
        // status hues
        "turmeric-gold": "#E8A33D",
        "chili-ember": "#D64933",
        "ash-taupe": "#9C9186",
        "smoked-brick": "#7A3B34",
      },
      fontFamily: {
        sans: ["var(--font-manrope)", "system-ui", "sans-serif"],
        display: ["var(--font-fraunces)", "serif"],
      },
    },
  },
  plugins: [],
};
export default config;
