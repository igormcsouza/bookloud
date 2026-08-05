// Server Component: ENVIRONMENT is deliberately a server-only runtime env var
// (not NEXT_PUBLIC_*, see infra/stacks/frontend_stack.py), so whether email
// is required has to be resolved here and handed down as a prop -- a client
// component can't read a non-NEXT_PUBLIC_* env var itself. See
// PLANS/phase-1.md §5.6/§10 Q3.
import SignupForm from "./SignupForm";

export default function SignupPage() {
  const emailRequired = process.env.ENVIRONMENT === "prod";
  return <SignupForm emailRequired={emailRequired} />;
}
