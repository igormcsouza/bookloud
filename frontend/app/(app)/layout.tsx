// Server component: the two-pane shell and nothing else (PLANS/phase-6.md
// §7.1/§7.2). A route group, so `(app)` never appears in a URL -- `/` stays
// the library and `/books/<id>` the reader, while `/login` and `/signup` sit
// outside the group and keep their own bare centred layout. `middleware.ts`'s
// existing matcher already gates `/books/*`; no middleware change.
//
// Why the shell is a server component but everything inside it is not: the id
// token lives in a browser module variable (`lib/auth.ts`), and the only
// credential the SSR Lambda ever sees is the httpOnly refresh cookie. A
// server component that wanted to call GET /books would have to read that
// cookie, call Cognito's InitiateAuth to mint an id token, and then call the
// API -- two extra network round trips on the render path of a Lambda
// competing for an account-wide budget of 10 concurrent executions, on every
// navigation, for data the client is about to start polling anyway.

import BookSidebar from "@/components/BookSidebar";
import { BooksProvider } from "@/components/BooksProvider";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <BooksProvider>
      <div className="flex h-screen">
        <BookSidebar />
        {children}
      </div>
    </BooksProvider>
  );
}
