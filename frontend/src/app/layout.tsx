import type { Metadata } from "next";
import { Fraunces, Manrope } from "next/font/google";
import "./globals.css";

// Manrope: warm-but-clean geometric sans for body/UI — legible at small sizes
// (order tables, tabular money) without reading like a generic SaaS default.
const manrope = Manrope({
  subsets: ["latin"],
  variable: "--font-manrope",
  display: "swap",
});

// Fraunces: warm editorial serif for the wordmark and page headings — the
// "confident, not childish" display voice called out in the design sign-off.
const fraunces = Fraunces({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-fraunces",
  display: "swap",
});

export const metadata: Metadata = {
  title: "AbhiAya",
  description: "AbhiAya restaurant & admin dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${manrope.variable} ${fraunces.variable} min-h-screen bg-roasted-almond font-sans text-cast-iron antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
