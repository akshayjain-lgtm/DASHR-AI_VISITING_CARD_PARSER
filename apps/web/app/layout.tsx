import type { Metadata } from "next";
import { Manrope, DM_Mono } from "next/font/google";
import "./globals.css";

const manrope = Manrope({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-manrope",
});

const dmMono = DM_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-dm-mono",
});

export const metadata: Metadata = {
  title: "DASHR AI — Turn Business Cards Into Your Best Lead List",
  description:
    "Upload hundreds of visiting cards from your last trade show. DASHR AI extracts every detail, enriches with company intelligence, and scores each lead by fit for your product lines.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={`${manrope.variable} ${dmMono.variable}`}>
      <body className="min-h-screen bg-white font-sans antialiased">{children}</body>
    </html>
  );
}
