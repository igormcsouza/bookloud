// Shared shell for the login/signup pages -- visual language copied from
// app/page.tsx (slate-950 bg, indigo-300 headings, slate-900 card).

export const inputClass =
  "w-full bg-slate-950 border border-slate-700 text-slate-100 " +
  "placeholder:text-slate-500 rounded-lg px-3 py-2 focus:outline-none " +
  "focus:ring-2 focus:ring-indigo-400 focus:border-indigo-400";

export const labelClass = "block text-sm font-medium text-slate-300 mb-1";

type AuthCardProps = {
  title: string;
  subtitle?: string;
  error?: string | null;
  children: React.ReactNode;
};

export default function AuthCard({ title, subtitle, error, children }: AuthCardProps) {
  return (
    <div className="max-w-4xl mx-auto py-10 px-4">
      <h1 className="text-4xl font-bold text-indigo-300 text-center mb-6">Bookloud</h1>

      <div className="bg-slate-900 border border-slate-800 rounded-xl shadow-2xl shadow-black/50 p-6 w-full max-w-md mx-auto">
        <h2 className="text-xl font-bold text-indigo-300 mb-1">{title}</h2>
        {subtitle && <p className="text-sm text-slate-400 mb-4">{subtitle}</p>}

        {error && (
          <div
            role="alert"
            className="bg-red-500/15 text-red-300 ring-1 ring-red-500/25 rounded-lg px-4 py-3 mb-4"
          >
            {error}
          </div>
        )}

        {children}
      </div>
    </div>
  );
}
