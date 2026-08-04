import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Bookloud",
  description: "Read your PDFs aloud with word-level highlighting and a chat sidebar.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-slate-950 text-slate-100 min-h-screen">{children}</body>
    </html>
  );
}
