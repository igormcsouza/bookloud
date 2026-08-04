/** Base URL of the Bookloud backend API, trimmed of any trailing slash. */
export const apiUrl = (path: string): string =>
  `${(process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "")}${path}`;
