// Bridges lib/api.ts and lib/chat.ts (no navigation concerns) to the root
// layout's auth guard: a 401 means the session is gone for good, so the
// layout listens here and redirects to sign-in -- the RN equivalent of the
// web app's `window.location.href = "/login"` (frontend/lib/api.ts).

type Listener = () => void;

let listener: Listener | null = null;

export function onSessionExpired(cb: Listener | null) {
  listener = cb;
}

export function notifySessionExpired() {
  listener?.();
}
