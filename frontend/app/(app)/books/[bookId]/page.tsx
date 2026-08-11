// Server shell; every data-touching component below it is "use client"
// (PLANS/phase-6.md §7.2). No server-side fetch against the API anywhere in
// this phase, and no loading.tsx streaming.

import ReaderView from "@/components/ReaderView";

export default async function ReaderPage({
  params,
}: {
  params: Promise<{ bookId: string }>;
}) {
  const { bookId } = await params;
  return <ReaderView bookId={bookId} />;
}
