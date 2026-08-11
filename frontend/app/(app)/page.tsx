// "/" -- the library. The sidebar (in the route group's layout) does the
// listing; this pane is the empty state beside it.

export default function LibraryPage() {
  return (
    <main data-testid="library-empty" className="flex flex-1 items-center justify-center px-8">
      <div className="max-w-md text-center">
        <h2 className="mb-3 text-2xl font-bold text-moss-300">Your library</h2>
        <p className="text-sage">
          Pick a book from the sidebar, or upload a PDF to have it read aloud with the
          words highlighted as they are spoken.
        </p>
      </div>
    </main>
  );
}
