import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "LITMUS — paper auditor",
  description:
    "A self-extending, multi-domain instrument for auditing published scientific papers with executable evidence.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header
          className="border-b"
          style={{ borderColor: "var(--border)", background: "var(--surface)" }}
        >
          <div className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-5 py-3">
            <Link href="/" className="flex items-baseline gap-2 no-underline">
              <span className="text-base font-semibold tracking-tight">LITMUS</span>
              <span
                className="hidden text-xs sm:inline"
                style={{ color: "var(--faint)" }}
              >
                executable evidence for scientific claims
              </span>
            </Link>
            <nav className="flex items-center gap-1 text-sm">
              <Link
                href="/"
                className="rounded-md px-3 py-1.5 no-underline hover:bg-[var(--surface-2)]"
              >
                Gallery
              </Link>
              <Link
                href="/upload"
                className="rounded-md px-3 py-1.5 no-underline hover:bg-[var(--surface-2)]"
              >
                Upload
              </Link>
            </nav>
          </div>
        </header>

        <main className="mx-auto w-full max-w-5xl flex-1 px-5 py-8">{children}</main>

        <footer
          className="border-t"
          style={{ borderColor: "var(--border)" }}
        >
          <div
            className="mx-auto max-w-5xl px-5 py-4 text-xs"
            style={{ color: "var(--faint)" }}
          >
            Every flag ships a recompute script you can run yourself. Subjective
            dimensions are surfaced, not scored.
          </div>
        </footer>
      </body>
    </html>
  );
}
