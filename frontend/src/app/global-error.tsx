"use client";
import ErrorPage from "./error";

export default function GlobalError({ error, retry }: { error: unknown; retry: () => void }) {
  return <html lang="en"><body><ErrorPage error={error} retry={retry} /></body></html>;
}
