"use client";
import { Button } from "@/components/ui/button";
export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <main className="mx-auto w-full max-w-6xl p-6"><h1 className="text-2xl font-semibold">Something went wrong</h1><p role="alert" className="my-4">This page could not be displayed. Try loading it again.</p><Button onClick={reset}>Try again</Button></main>;
}
