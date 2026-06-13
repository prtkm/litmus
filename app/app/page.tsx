import Link from "next/link";
import { listPapers, usingLiveData } from "@/lib/data";
import { PaperCard } from "@/components/paper-card";

// The gallery is request-time on Supabase (live data), prerenderable on
// fixtures. Mark dynamic so a deploy with env vars always reads fresh papers.
export const dynamic = "force-dynamic";

export default async function GalleryPage() {
  const papers = await listPapers();
  const live = usingLiveData();

  return (
    <div>
      <section className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Audited papers</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed" style={{ color: "var(--muted)" }}>
          Each paper below has been run through the LITMUS audit. Open one to see
          the checkable flags — every one carries a trust tier and a recompute
          script you can run in your browser — alongside the dimensions routed to
          a human and the false positives the pipeline caught and dropped.
        </p>
        <div className="mt-3 flex items-center gap-3 text-xs" style={{ color: "var(--faint)" }}>
          <span>
            {papers.length} paper{papers.length === 1 ? "" : "s"}
          </span>
          <span aria-hidden>·</span>
          <span>{live ? "live (Supabase)" : "local fixtures"}</span>
          <span aria-hidden>·</span>
          <Link href="/upload" className="underline">
            Audit a new paper
          </Link>
        </div>
      </section>

      {papers.length === 0 ? (
        <div
          className="rounded-xl border p-8 text-center text-sm"
          style={{ borderColor: "var(--border)", color: "var(--muted)" }}
        >
          No papers yet.{" "}
          <Link href="/upload" className="underline">
            Upload the first one.
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          {papers.map((p) => (
            <PaperCard key={p.id} paper={p} />
          ))}
        </div>
      )}
    </div>
  );
}
